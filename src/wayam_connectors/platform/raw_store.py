"""Retained raw evidence, encrypted at rest and keyed by tenant/connection/sha256."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from wayam_connectors.contracts.errors import PolicyError

from .credentials import SecretBackend


class LocalEncryptedRawStore:
    """Filesystem backend for development. Production uses encrypted object storage behind the same interface."""

    def __init__(self, root: Path, backend: SecretBackend) -> None:
        self.root = root
        self.backend = backend

    def _path(self, tenant_id: UUID, connection_id: UUID, sha256: str) -> Path:
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
            raise PolicyError("invalid object hash")
        return self.root / str(tenant_id) / str(connection_id) / sha256[:2] / sha256

    def put(self, tenant_id: UUID, connection_id: UUID, data: bytes) -> tuple[str, str]:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(tenant_id, connection_id, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        ciphertext, key_id = self.backend.encrypt(data, f"{tenant_id}|{connection_id}|{sha}".encode())
        path.write_bytes(key_id.encode() + b"\n" + ciphertext)
        return sha, f"{tenant_id}/{connection_id}/{sha}"

    def get(self, tenant_id: UUID, connection_id: UUID, sha256: str) -> bytes:
        path = self._path(tenant_id, connection_id, sha256)
        if not path.is_file():
            raise PolicyError("raw object not found for this tenant/connection")
        key_id, _, ciphertext = path.read_bytes().partition(b"\n")
        return self.backend.decrypt(ciphertext, f"{tenant_id}|{connection_id}|{sha256}".encode(), key_id.decode())

    def delete(self, tenant_id: UUID, connection_id: UUID, sha256: str) -> bool:
        path = self._path(tenant_id, connection_id, sha256)
        if path.is_file():
            path.unlink()
            return True
        return False
