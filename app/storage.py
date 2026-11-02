import json
import re
import shutil
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

from . import config


def meta_path(fid: str):
    if not re.fullmatch(r"[a-f0-9]{32}", fid):
        raise HTTPException(400, "Bad file id")
    return config.STORAGE_DIR / f"{fid}.json", config.STORAGE_DIR / f"{fid}.bin"


def save(data: bytes, filename: str, content_type: str) -> dict:
    if len(data) > config.MAX_FILE_BYTES:
        raise HTTPException(413, f"File exceeds {config.MAX_FILE_BYTES // 1048576} MB limit")
    fid = uuid.uuid4().hex
    mp, bp = meta_path(fid)
    bp.write_bytes(data)
    meta = {"id": fid, "filename": filename, "content_type": content_type, "size": len(data),
            "download_url": f"/v1/files/{fid}/download"}
    mp.write_text(json.dumps(meta))
    return meta


def cleanup() -> None:
    """Delete expired (media) files."""
    now = time.time()
    for mp in config.STORAGE_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            if m.get("expires_at") and m["expires_at"] < now:
                mp.unlink()
                mp.with_suffix(".bin").unlink(missing_ok=True)
        except Exception:
            continue
    # Orphaned media work dirs (crashed/timed-out downloads) older than an hour
    import tempfile
    for d in Path(tempfile.gettempdir()).glob("lb_*"):
        try:
            if now - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def save_path(path, filename: str, content_type: str, ttl_minutes: int) -> dict:
    """Move a finished file on disk into storage (no RAM copy). Expires after ttl_minutes."""
    cleanup()
    fid = uuid.uuid4().hex
    mp, bp = meta_path(fid)
    size = path.stat().st_size
    shutil.move(str(path), str(bp))
    meta = {"id": fid, "filename": filename, "content_type": content_type, "size": size,
            "download_url": f"/v1/files/{fid}/download", "expires_at": time.time() + ttl_minutes * 60}
    mp.write_text(json.dumps(meta))
    return meta
