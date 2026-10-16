"""Credential references: secrets are encrypted at rest behind a reference and bound to tenant+connection.

The ciphertext's associated data is `tenant_id|connection_id`, so a row copied to
another tenant or connection cannot be decrypted. Rotation writes a new version
and revokes the old; revocation blocks the connection.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from wayam_connectors.contracts.errors import AuthError, NotConfiguredError

from .db import Store


class SecretBackend(Protocol):
    name: str

    def encrypt(self, plaintext: bytes, aad: bytes) -> tuple[bytes, str]: ...
    def decrypt(self, ciphertext: bytes, aad: bytes, key_id: str) -> bytes: ...


class LocalSecretBackend:
    """AES-256-GCM with a key from the environment. For development and tests only."""

    name = "local"

    def __init__(self, key_b64: str | None, key_id: str = "local-1") -> None:
        if not key_b64:
            raise NotConfiguredError("WAYAM_LOCAL_SECRET_KEY is not set")
        key = base64.b64decode(key_b64)
        if len(key) != 32:
            raise NotConfiguredError("WAYAM_LOCAL_SECRET_KEY must decode to 32 bytes")
        self._aes = AESGCM(key)
        self.key_id = key_id

    def encrypt(self, plaintext: bytes, aad: bytes) -> tuple[bytes, str]:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, plaintext, aad), self.key_id

    def decrypt(self, ciphertext: bytes, aad: bytes, key_id: str) -> bytes:
        if key_id != self.key_id:
            raise AuthError("credential was encrypted with an unknown key", details={"key_id": key_id})
        try:
            return self._aes.decrypt(ciphertext[:12], ciphertext[12:], aad)
        except Exception as exc:  # cryptography raises InvalidTag
            raise AuthError("credential cannot be decrypted for this tenant/connection") from exc


class AzureKeyVaultBackend:
    """Production backend placeholder. Not implemented: raises rather than pretending.

    Decision (ADR-0003): wrap keys with Key Vault once the Azure SDK dependency and
    managed-identity environment are available; until then production cannot start
    with this backend selected.
    """

    name = "azure_key_vault"

    def __init__(self, vault_url: str | None) -> None:
        raise NotConfiguredError("Azure Key Vault backend is not implemented yet (see docs/decisions/0003)")

    def encrypt(self, plaintext: bytes, aad: bytes) -> tuple[bytes, str]:
        raise NotConfiguredError("Azure Key Vault backend is not implemented")

    def decrypt(self, ciphertext: bytes, aad: bytes, key_id: str) -> bytes:
        raise NotConfiguredError("Azure Key Vault backend is not implemented")


def backend_from_settings(secret_backend: str, local_key: str | None, vault_url: str | None) -> SecretBackend:
    if secret_backend == "local":
        return LocalSecretBackend(local_key)
    if secret_backend == "azure_key_vault":
        return AzureKeyVaultBackend(vault_url)
    raise NotConfiguredError(f"unknown secret backend {secret_backend!r}")


def _aad(tenant_id: UUID, connection_id: UUID) -> bytes:
    return f"{tenant_id}|{connection_id}".encode()


class CredentialStore:
    def __init__(self, store: Store, backend: SecretBackend) -> None:
        self.store = store
        self.backend = backend

    def put(self, tenant_id: UUID, connection_id: UUID, secret: dict[str, Any]) -> UUID:
        """Store a new credential version and revoke the previous one (rotation)."""
        ciphertext, key_id = self.backend.encrypt(json.dumps(secret).encode(), _aad(tenant_id, connection_id))
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM credential_ref WHERE tenant_id=%s AND connection_id=%s",
                (tenant_id, connection_id),
            )
            version = int(cur.fetchone()["v"]) + 1  # type: ignore[index]
            cur.execute(
                "UPDATE credential_ref SET revoked_at = now(), rotated_at = now() "
                "WHERE tenant_id=%s AND connection_id=%s AND revoked_at IS NULL",
                (tenant_id, connection_id),
            )
            cur.execute(
                "INSERT INTO credential_ref (tenant_id, connection_id, backend, key_id, ciphertext, version) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant_id, connection_id, self.backend.name, key_id, ciphertext, version),
            )
            return UUID(str(cur.fetchone()["id"]))  # type: ignore[index]

    def get(self, tenant_id: UUID, connection_id: UUID) -> dict[str, Any]:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT key_id, ciphertext FROM credential_ref WHERE tenant_id=%s AND connection_id=%s AND revoked_at IS NULL",
                (tenant_id, connection_id),
            )
            row = cur.fetchone()
        if row is None:
            raise AuthError("no active credential for connection", details={"connection_id": str(connection_id)})
        plaintext = self.backend.decrypt(bytes(row["ciphertext"]), _aad(tenant_id, connection_id), row["key_id"])
        return dict(json.loads(plaintext))

    def revoke(self, tenant_id: UUID, connection_id: UUID, reason: str) -> None:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "UPDATE credential_ref SET revoked_at = now() WHERE tenant_id=%s AND connection_id=%s AND revoked_at IS NULL",
                (tenant_id, connection_id),
            )
            cur.execute(
                "UPDATE connection SET status='revoked', status_reason=%s, revoked_at=now(), updated_at=now() WHERE tenant_id=%s AND id=%s",
                (reason, tenant_id, connection_id),
            )
            cur.execute(
                "INSERT INTO audit_event (tenant_id, actor, action, target, details) VALUES (%s, 'system', 'credential.revoke', %s, %s)",
                (tenant_id, str(connection_id), json.dumps({"reason": reason, "at": datetime.now(UTC).isoformat()})),
            )
