"""Developer/operator CLI: `wayam-connectors <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wayam_connectors.contracts.identity import Product


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wayam-connectors")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations to WAYAM_DATABASE_URL")
    sub.add_parser("validate-manifests", help="load every manifest and check adapter conformance")
    sub.add_parser("validate-catalog", help="cross-check manifests, registry.v1.json and the research catalog")
    sub.add_parser("sync-definitions", help="upsert connector_definition rows from manifests")
    ct = sub.add_parser("create-tenant", help="create a tenant and print its id")
    ct.add_argument("name")
    ik = sub.add_parser("issue-key", help="issue an API key (printed once)")
    ik.add_argument("tenant_id")
    ik.add_argument("product", choices=[x.value for x in Product])
    ik.add_argument("--admin", action="store_true")
    oa = sub.add_parser("export-openapi", help="write the OpenAPI document")
    oa.add_argument("path", nargs="?", default="packages/contracts/openapi.json")
    sv = sub.add_parser("serve", help="run the control API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8090)
    wk = sub.add_parser("worker", help="run the job worker")
    wk.add_argument("--worker-id", default="worker-1")
    args = p.parse_args(argv)

    if args.cmd == "validate-manifests":
        from wayam_connectors.platform.registry import load_registry

        reg = load_registry(strict=True)
        for cid, e in sorted(reg.items()):
            caps = [c.value for c in e.manifest.capabilities]
            print(f"{cid:32} v{e.manifest.adapter_version:8} {e.manifest.verification_level.value:28} {caps}")
        return 0
    if args.cmd == "validate-catalog":
        from wayam_connectors.platform.registry import load_registry, validate_against_catalog

        problems = validate_against_catalog(load_registry(strict=True))
        for pr in problems:
            print("PROBLEM:", pr)
        print("catalog OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if args.cmd == "export-openapi":
        from wayam_connectors.api.app import create_app

        out = Path(args.path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(create_app().openapi(), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
        return 0

    from wayam_connectors.platform.runtime import build_runtime

    if args.cmd == "migrate":
        from wayam_connectors.platform.config import get_settings
        from wayam_connectors.platform.db import Store

        applied = Store(get_settings().database_url).migrate()
        print("applied:", applied or "nothing")
        return 0
    rt = build_runtime()
    if args.cmd == "sync-definitions":
        from wayam_connectors.platform.registry import sync_definitions

        print("definitions:", sync_definitions(rt.store, rt.registry))
        return 0
    if args.cmd == "create-tenant":
        print(rt.tenants.create_tenant(args.name))
        return 0
    if args.cmd == "issue-key":
        from uuid import UUID

        key = rt.tenants.issue_api_key(UUID(args.tenant_id), Product(args.product), ["admin"] if args.admin else [])
        print(key)
        return 0
    if args.cmd == "serve":
        import uvicorn

        from wayam_connectors.api.app import create_app
        from wayam_connectors.platform.registry import sync_definitions

        sync_definitions(rt.store, rt.registry)
        uvicorn.run(create_app(rt), host=args.host, port=args.port)
        return 0
    if args.cmd == "worker":
        from wayam_connectors.worker.main import DEFAULT_HANDLERS, serve

        serve(rt, DEFAULT_HANDLERS, args.worker_id)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
