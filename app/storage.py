import json
import re
import uuid

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
