"""Start the bgutil PO-token provider from inside the app.

scripts/start.sh does this too, but only when Render actually runs it as the start command. A service created in the
dashboard (or with a different start command) boots plain uvicorn, the provider never starts, and YouTube sees every
request without a PO token. Doing it here makes the fix independent of how the process is launched.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import config

ROOT = Path(__file__).resolve().parent.parent
DEST = Path(os.getenv("BGUTIL_HOME") or ROOT / ".bgutil")
PORT = int(os.getenv("BGUTIL_PORT", "4416"))
state: dict = {"phase": "idle", "detail": None}
_proc = None


def _deno() -> str | None:
    p = shutil.which("deno")
    if p:
        return p
    try:
        import deno
        p = deno.find_deno_bin()
        return p if p and os.access(p, os.X_OK) else None
    except Exception:  # noqa: BLE001
        return None


def _ping(url: str, t: float = 2) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/ping", timeout=t):
            return True
    except Exception:  # noqa: BLE001
        return False


def _run():
    global _proc
    try:
        url = f"http://127.0.0.1:{PORT}"
        if _ping(url):  # already running (started by scripts/start.sh)
            config.YT_POT_PROVIDER_URL = config.YT_POT_PROVIDER_URL or url
            state.update(phase="ready", detail="already running")
            return
        deno = _deno()
        if not deno:
            state.update(phase="failed", detail="deno binary not found (pip package 'deno' missing?)")
            return
        if not (DEST / "server" / "src" / "main.ts").exists():
            state.update(phase="installing", detail="provider not built at deploy time; installing now")
            r = subprocess.run(["bash", str(ROOT / "scripts" / "setup_pot.sh")], capture_output=True, text=True,
                               timeout=420, env={**os.environ, "BGUTIL_HOME": str(DEST)})
            if r.returncode != 0:
                state.update(phase="failed", detail="setup failed: " + (r.stderr or r.stdout)[-300:])
                return
        if not (DEST / "server" / "src" / "main.ts").exists():
            state.update(phase="failed", detail="main.ts missing after setup")
            return
        log = open("/tmp/pot.log", "ab")
        _proc = subprocess.Popen([deno, "run", "--allow-all", "src/main.ts", "--port", str(PORT)],
                                 cwd=str(DEST / "server"), stdout=log, stderr=subprocess.STDOUT)
        state.update(phase="starting", detail=f"pid {_proc.pid}")
        for _ in range(90):
            if _proc.poll() is not None:
                state.update(phase="failed", detail=f"provider exited with code {_proc.returncode}")
                return
            if _ping(url):
                config.YT_POT_PROVIDER_URL = config.YT_POT_PROVIDER_URL or url
                state.update(phase="ready", detail=f"pid {_proc.pid}")
                return
            time.sleep(1)
        state.update(phase="failed", detail="provider did not answer /ping within 90s")
    except Exception as e:  # noqa: BLE001
        state.update(phase="failed", detail=f"{type(e).__name__}: {str(e)[:200]}")


def ensure_provider() -> None:
    """Fire and forget: never delays or breaks app startup."""
    if config.YT_POT_PROVIDER_URL or os.getenv("POT_AUTOSTART", "1") == "0":
        state.update(phase="skipped", detail="YT_POT_PROVIDER_URL already set or POT_AUTOSTART=0")
        return
    threading.Thread(target=_run, name="pot-provider", daemon=True).start()
