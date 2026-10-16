"""Product journey: Bodham cited document fragments behind source ACLs (T-27, T-28, T-29, T-30, T-31, T-35)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import FIXTURES
from wayam_connectors.contracts.domains.knowledge import SourcePermission
from wayam_connectors.contracts.errors import PolicyError
from wayam_connectors.contracts.identity import Product
from wayam_connectors.platform.connections import ConnectionService
from wayam_connectors.platform.credentials import CredentialStore
from wayam_connectors.platform.file_import import FileImportService
from wayam_connectors.platform.knowledge_access import KnowledgeAccessService
from wayam_connectors.platform.records import RecordStore

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


def _perm(doc: str, principal: str, role: str = "read", inherited: bool = False) -> SourcePermission:
    return SourcePermission(
        source_system="fixture",
        source_object_id=f"{doc}:{principal}",
        observed_at_iso="2026-10-16T00:00:00+00:00",
        document_id=doc,
        principal_id=principal,
        role=role,
        inherited=inherited,
        acl_version="v1",
    )


@pytest.fixture
def world(store, registry, grants, backend, tenants):
    from tests.conftest import make_tenant

    t = make_tenant(tenants, "bodham-tenant", Product.BODHAM)
    cid = ConnectionService(store).create(t.admin, "files.docx", "sharepoint fixture library", {})
    grants.grant(t.admin, cid, Product.BODHAM, ["FileParser"])
    svc = FileImportService(registry, grants, RecordStore(store), None)
    access = KnowledgeAccessService(store, grants)
    docx = (FIXTURES / "office" / "review.docx").read_bytes()
    pptx = (FIXTURES / "office" / "deck.pptx").read_bytes()
    r1 = svc.import_file(
        t.product_ctx,
        cid,
        connector_id="files.docx",
        data=docx,
        source_ref="sp://site/Shared/review.docx",
        parse_options=None,
        domain_mapping="knowledge.fragment",
        mapping_config={"document_id": "doc-review"},
    )
    cid_pptx = cid  # same connection; pptx connector is a different parser but the connection is a library of files
    ConnectionService(store)  # noqa: B018
    r2 = svc.import_file(
        t.product_ctx,
        cid_pptx,
        connector_id="files.pptx",
        data=pptx,
        source_ref="sp://site/Private/deck.pptx",
        parse_options=None,
        domain_mapping="knowledge.fragment",
        mapping_config={"document_id": "doc-deck"},
    )
    assert r1["stored"] == 7 and r2["stored"] >= 5 and r2["summary"]["complete"] is False
    access.upsert_principals(
        t.tenant_id,
        cid,
        [
            {"principal_id": "u:alice", "kind": "user", "email": "alice@example.invalid", "member_of": ["g:finance"]},
            {"principal_id": "u:bob", "kind": "user", "member_of": ["g:eng"]},
            {"principal_id": "g:finance", "kind": "group", "member_of": ["g:all-staff"]},
            {"principal_id": "g:eng", "kind": "group", "member_of": ["g:all-staff"]},
            {"principal_id": "g:all-staff", "kind": "group"},
        ],
    )
    access.map_principal(t.tenant_id, cid, "bodham-user:alice", "u:alice")
    access.map_principal(t.tenant_id, cid, "bodham-user:bob", "u:bob")
    access.map_principal(t.tenant_id, cid, "bodham-user:ghost", "u:ghost")  # mapped to an identity the source never reported
    access.replace_document_acl(t.tenant_id, cid, "doc-review", [_perm("doc-review", "g:all-staff", inherited=True)], acl_version="v1")
    access.replace_document_acl(t.tenant_id, cid, "doc-deck", [_perm("doc-deck", "g:finance")], acl_version="v1")
    return t, cid, access


def _docs(frags):
    return sorted({f["document_id"] for f in frags})


def test_private_shared_nested_groups_and_citations(world):
    t, cid, access = world
    alice = access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:alice")
    assert _docs(alice) == ["doc-deck", "doc-review"]  # shared via nested group + private via finance
    deck = [f for f in alice if f["document_id"] == "doc-deck"]
    assert any(f["citation"] == "slide 1" and f["text"] in ("Roadmap 2027", "Launch SAMYAK pilot") for f in deck)
    assert any(f["completeness"] == "incomplete" and f["citation"] == "slide 3" for f in deck)
    review = [f for f in alice if f["document_id"] == "doc-review"]
    assert review[0]["granted_via"] == "g:all-staff" and review[0]["provenance"].startswith("sp://site/Shared/review.docx#")
    bob = access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:bob")
    assert _docs(bob) == ["doc-review"]  # T-27: private deck hidden
    assert access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:ghost") == []  # T-29 unknown identity
    assert access.visible_fragments(t.product_ctx, cid, wayam_principal="nobody") == []  # unmapped


def test_group_removal_link_change_and_deletion(world):
    t, cid, access = world
    # T-28: alice removed from finance -> deck disappears
    access.upsert_principals(t.tenant_id, cid, [{"principal_id": "u:alice", "kind": "user", "member_of": []}])
    assert _docs(access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:alice")) == []  # also lost all-staff via finance
    # T-30: ACL replaced with a sharing link principal -> only link holders; permission removal takes effect immediately
    access.replace_document_acl(t.tenant_id, cid, "doc-review", [_perm("doc-review", "link:anyone-with-link")], acl_version="v2")
    assert _docs(access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:bob")) == []
    access.upsert_principals(
        t.tenant_id,
        cid,
        [
            {"principal_id": "u:bob", "kind": "user", "member_of": ["g:eng", "link:anyone-with-link"]},
            {"principal_id": "link:anyone-with-link", "kind": "link"},
        ],
    )
    bob = access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:bob")
    assert _docs(bob) == ["doc-review"] and bob[0]["acl_version"] == "v2"
    # deleted source document -> hidden even though fragments still exist until retention cleanup
    access.mark_document_deleted(t.tenant_id, cid, "doc-review")
    assert access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:bob") == []


def test_stale_acl_fails_closed_and_revocation_blocks(world, store, backend):
    t, cid, access = world
    later = datetime.now(UTC) + timedelta(minutes=16)
    # T-29: ACL evidence older than 15 minutes is withheld until revalidated
    assert access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:alice", now=later) == []
    assert access.stale_documents(t.tenant_id, cid, now=later) == ["doc-deck", "doc-review"]
    access.replace_document_acl(t.tenant_id, cid, "doc-deck", [_perm("doc-deck", "g:finance")], acl_version="v3", confirmed_at=later)
    assert _docs(access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:alice", now=later)) == ["doc-deck"]
    # T-31: revoked connection blocks immediately
    CredentialStore(store, backend).revoke(t.tenant_id, cid, "admin disconnected the library")
    with pytest.raises(PolicyError):
        access.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:alice")
