"""Keep yt-dlp fresh. YouTube changes its player every few weeks and a stale yt-dlp is the #1 cause of sudden
"blocked" / "format not available" errors. At startup (before yt_dlp is imported) we install the newest nightly build
into a writable folder and put it first on sys.path. If anything fails we silently keep the version from requirements.txt.
Env: YTDLP_AUTOUPDATE=0 turns it off, YTDLP_CHANNEL=stable|nightly (default nightly), YTDLP_UPDATE_DIR."""
import os
import subprocess
import sys
import time
from pathlib import Path

TARGET = Path(os.getenv("YTDLP_UPDATE_DIR", "/tmp/ytdlp-latest"))
STAMP = TARGET / ".stamp"
MAX_AGE = 6 * 3600  # do not re-install more than every 6h when the process restarts often


def _pip(*args: str, timeout: int = 90) -> bool:
    import importlib.util
    import shutil
    if importlib.util.find_spec("pip"):
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-input",
               "--upgrade", "--target", str(TARGET), *args]
    elif shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--quiet", "--python", sys.executable, "--upgrade", "--target", str(TARGET), *args]
    else:
        return False
    try:
        return subprocess.run(cmd, timeout=timeout, capture_output=True).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def ensure_latest() -> None:
    if os.getenv("YTDLP_AUTOUPDATE", "1") == "0":
        return
    try:
        fresh = STAMP.exists() and time.time() - STAMP.stat().st_mtime < MAX_AGE and (TARGET / "yt_dlp").exists()
        if not fresh:
            TARGET.mkdir(parents=True, exist_ok=True)
            pre = ["--pre"] if os.getenv("YTDLP_CHANNEL", "nightly") == "nightly" else []
            # --no-deps so a pre-release flag never drags in unstable copies of other libraries
            ok = _pip(*pre, "--no-deps", "yt-dlp")
            _pip("--no-deps", "yt-dlp-ejs")  # the JS challenge solver; must be recent too
            if ok:
                STAMP.write_text(str(time.time()))
        if (TARGET / "yt_dlp").exists():
            sys.path.insert(0, str(TARGET))
    except Exception:  # noqa: BLE001
        pass


def version() -> str | None:
    try:
        import yt_dlp.version as v
        return v.__version__
    except Exception:  # noqa: BLE001
        return None
