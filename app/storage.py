import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

from . import config


def public_base() -> str:
    """Public origin for absolute download links (Render sets RENDER_EXTERNAL_URL)."""
    return (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")


def file_url(fid: str) -> str:
    """Absolute, self-authenticating download URL. The 128-bit id is the capability."""
    return f"{public_base()}/v1/files/{fid}/download"


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
            "download_url": file_url(fid)}
    mp.write_text(json.dumps(meta))
    return meta


def _media_files():
    """(mtime, size, json_path) of every expiring (downloaded) file. Files you uploaded yourself have no expires_at and are never touched."""
    out = []
    for mp in config.STORAGE_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            if m.get("expires_at"):
                bp = mp.with_suffix(".bin")
                out.append((mp.stat().st_mtime, bp.stat().st_size if bp.exists() else 0, mp))
        except Exception:
            continue
    return sorted(out)


def _drop(mp) -> int:
    n = 0
    bp = mp.with_suffix(".bin")
    if bp.exists():
        n = bp.stat().st_size
    bp.unlink(missing_ok=True)
    mp.unlink(missing_ok=True)
    return n


def cleanup(force: bool = False) -> dict:
    """Keep the disk clear. Removes: expired downloads, orphaned .bin files, abandoned temp dirs, and (when storage is
    over MAX_STORAGE_MB or the disk is nearly full, or force=True) the oldest downloads until there is room again."""
    now = time.time()
    freed = 0
    removed = 0
    for mp in config.STORAGE_DIR.glob("*.json"):
        try:
            m = json.loads(mp.read_text())
            if m.get("expires_at") and m["expires_at"] < now:
                freed += _drop(mp)
                removed += 1
        except Exception:
            continue
    for bp in config.STORAGE_DIR.glob("*.bin"):  # data without metadata (crashed mid-save)
        try:
            if not bp.with_suffix(".json").exists() and now - bp.stat().st_mtime > 600:
                freed += bp.stat().st_size
                bp.unlink()
                removed += 1
        except OSError:
            continue
    import tempfile
    for d in Path(tempfile.gettempdir()).glob("lb_*"):  # abandoned work dirs from failed/timed-out downloads
        try:
            if now - d.stat().st_mtime > 300:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue
    files = _media_files()
    total = sum(f[1] for f in files)
    try:
        free_disk = shutil.disk_usage(config.STORAGE_DIR).free
    except OSError:
        free_disk = None
    cap = config.MAX_STORAGE_MB * 1048576
    low_disk = free_disk is not None and free_disk < config.MIN_FREE_MB * 1048576
    if force or total > cap or low_disk:
        target = 0 if (force or low_disk) else int(cap * 0.7)
        for _, size, mp in files:  # oldest first
            if total <= target:
                break
            freed += _drop(mp)
            total -= size
            removed += 1
    return {"removed": removed, "freed_mb": round(freed / 1048576, 1), "media_mb_left": round(total / 1048576, 1),
            "disk_free_mb": round(free_disk / 1048576) if free_disk is not None else None}


def save_path(path, filename: str, content_type: str, ttl_minutes: int) -> dict:
    """Move a finished file on disk into storage (no RAM copy). Expires after ttl_minutes."""
    cleanup()
    fid = uuid.uuid4().hex
    mp, bp = meta_path(fid)
    size = path.stat().st_size
    shutil.move(str(path), str(bp))
    meta = {"id": fid, "filename": filename, "content_type": content_type, "size": size,
            "download_url": file_url(fid), "expires_at": time.time() + ttl_minutes * 60}
    mp.write_text(json.dumps(meta))
    return meta
