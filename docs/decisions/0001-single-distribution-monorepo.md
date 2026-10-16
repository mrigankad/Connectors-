# ADR-0001: One Python distribution with package-per-concern, not a uv workspace

Date: 2026-10-16. Status: accepted.

The target repository (`mrigankad/Connectors-`) was empty. The build prompt suggests `apps/api`, `apps/worker`,
`packages/contracts`, `connectors/<provider>/<surface>`, `mappings/...`. We keep those names as subpackages of one
distribution `wayam-connectors` (`src/wayam_connectors/{contracts,platform,connectors,mappings,api,worker}`) instead
of separate installable packages.

Why: one lockfile, one test run, no cross-package version skew while the contracts are still moving. Splitting
`contracts` (and the generated SDKs) into their own distributions is a mechanical change once the API contract
stabilises; `packages/contracts/openapi.json` is already the published boundary.

Consequences: adapters import platform helpers directly (e.g. file limits); the registry enforces that adapters
only expose declared capabilities so the shared process does not become an escape hatch.
