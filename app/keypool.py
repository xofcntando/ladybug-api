"""Small round-robin pool for third-party API keys.

* Keys come from an env var (comma / space / newline separated) and optionally a file (one per line).
* `dead` keys (rejected as invalid) are skipped until restart; `cool` keys (out of quota / rate limited)
  are skipped for a while and then tried again.
* Nothing here ever returns a key value except `ordered()`, which is only for the caller's outbound request.
"""
import os
import re
import time


def load_keys(env: str, file_env: str | None = None, limit: int = 10000) -> list[str]:
    raw = os.getenv(env, "")
    path = os.getenv(file_env, "").strip() if file_env else ""
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                raw += "\n" + fh.read()
        except OSError:
            pass
    return list(dict.fromkeys(k for k in re.split(r"[,\s]+", raw) if k))[:limit]


class KeyPool:
    def __init__(self, keys: list[str]):
        self.keys = list(keys)
        self.dead: set[str] = set()
        self.cool: dict[str, float] = {}  # key -> monotonic time until which it is skipped
        self._rr = 0
        self.last_error: str | None = None

    @classmethod
    def from_env(cls, env: str, file_env: str | None = None, limit: int = 10000) -> "KeyPool":
        return cls(load_keys(env, file_env, limit))

    def usable(self) -> list[str]:
        now = time.monotonic()
        return [k for k in self.keys if k not in self.dead and self.cool.get(k, 0) <= now]

    def enabled(self) -> bool:
        return bool(self.usable())

    def configured(self) -> bool:
        return bool(self.keys)

    def ordered(self, max_tries: int = 5) -> list[str]:
        """Usable keys starting at the round-robin cursor, so load spreads over the whole pool."""
        keys = self.usable()
        if not keys:
            return []
        start = self._rr % len(keys)
        self._rr += 1
        return (keys[start:] + keys[:start])[:max_tries]

    def mark_dead(self, key: str, why: str | None = None) -> None:
        self.dead.add(key)
        if why:
            self.last_error = why

    def mark_cool(self, key: str, seconds: float, why: str | None = None) -> None:
        self.cool[key] = time.monotonic() + seconds
        if why:
            self.last_error = why

    def status(self) -> dict:
        now = time.monotonic()
        return {"keys": len(self.keys), "usable": len(self.usable()), "invalid": len(self.dead),
                "cooling_down": sum(1 for k, t in self.cool.items() if t > now and k not in self.dead),
                "last_error": self.last_error}
