"""Storage for uploaded source files. Local disk for now; swap for S3-compatible storage later
without touching callers. Originals are always kept so every record traces back to its source."""

import hashlib
import re
import uuid
from pathlib import Path

from app.core.config import get_settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def save_upload(business_id: uuid.UUID, filename: str, data: bytes) -> tuple[str, str]:
    """Persist bytes; return (storage_path, sha256)."""
    digest = hashlib.sha256(data).hexdigest()
    safe_name = _SAFE.sub("_", Path(filename).name)[:120] or "upload"
    root = get_settings().upload_dir
    folder = root / str(business_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}_{safe_name}"
    path.write_bytes(data)
    return str(path), digest
