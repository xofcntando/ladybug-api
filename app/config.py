import os
from pathlib import Path

API_KEYS = {k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()}
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "./storage"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_MB", "25")) * 1024 * 1024
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Our own per-API-key daily quota (not a third-party limit). 100000 requests / 24h by default.
DAILY_REQUEST_LIMIT = int(os.getenv("DAILY_REQUEST_LIMIT", "100000"))
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "1") != "0"

MEDIA_MAX_MB = int(os.getenv("MEDIA_MAX_MB", "100"))
MEDIA_MAX_MINUTES = int(os.getenv("MEDIA_MAX_MINUTES", "20"))
MEDIA_TTL_MINUTES = int(os.getenv("MEDIA_TTL_MINUTES", "30"))
MAX_STORAGE_MB = int(os.getenv("MAX_STORAGE_MB", "400"))   # downloads beyond this are deleted oldest-first
MIN_FREE_MB = int(os.getenv("MIN_FREE_MB", "150"))          # if the disk has less free space than this, all downloads are deleted
YT_COOKIES = os.getenv("YT_COOKIES", "")  # optional: Netscape-format cookies text, helps with YouTube bot checks

IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
TTS_MODEL = os.getenv("TTS_MODEL", "tts-1")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

YT_PROXY = os.getenv("YT_PROXY", "")  # optional: http(s)/socks5 proxy URL(s), comma separated = rotated, e.g. residential proxies
YT_PO_TOKEN = os.getenv("YT_PO_TOKEN", "")  # optional: manual PO token(s), e.g. "web.gvs+TOKEN" (comma separated)
YT_POT_PROVIDER_URL = os.getenv("YT_POT_PROVIDER_URL", "")  # optional: bgutil PO-token provider server, e.g. http://host:4416

AI_PROVIDER = os.getenv("AI_PROVIDER", "").strip().lower()  # optional: groq | gemini | openai | anthropic | pollinations
POLLINATIONS_KEY = os.getenv("POLLINATIONS_API_KEY", "")     # optional; without it the keyless tier is used (1 request / 45s / IP)
SELF_PING = os.getenv("SELF_PING", "1") != "0"               # keep a free Render instance awake
