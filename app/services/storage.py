"""Original files on disk, addressed by their SHA-256 hash.

The hash is also the proof of integrity: if a file changed, its hash would no
longer match the one in the database (GoBD: documents must stay unchanged).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.config import settings


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path(org_id: int, digest: str) -> Path:
    return settings.data_dir / str(org_id) / digest[:2] / digest


def save(org_id: int, data: bytes) -> str:
    digest = sha256(data)
    path = _path(org_id, digest)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic: never a half-written file
        path.chmod(0o440)  # read-only
    return digest


def load(org_id: int, digest: str) -> bytes:
    data = _path(org_id, digest).read_bytes()
    if sha256(data) != digest:
        raise ValueError(f"Integrity check failed for document {digest}")
    return data
