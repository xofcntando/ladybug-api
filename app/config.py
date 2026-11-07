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

# Small-by-default downloads: 128 kbps MP3 and 360p progressive MP4 (no ffmpeg merge, so it is fast).
AUDIO_KBPS = int(os.getenv("AUDIO_KBPS", "128"))
VIDEO_QUALITY_DEFAULT = int(os.getenv("VIDEO_QUALITY_DEFAULT", "360"))

MEDIA_MAX_MB = int(os.getenv("MEDIA_MAX_MB", "50"))
VIDEO_MAX_MB = int(os.getenv("VIDEO_MAX_MB", "20"))  # hard cap for downloaded videos (MP4)
MEDIA_MAX_MINUTES = int(os.getenv("MEDIA_MAX_MINUTES", "12"))
MEDIA_TTL_MINUTES = int(os.getenv("MEDIA_TTL_MINUTES", "10"))
MAX_STORAGE_MB = int(os.getenv("MAX_STORAGE_MB", "150"))   # downloads beyond this are deleted oldest-first
MIN_FREE_MB = int(os.getenv("MIN_FREE_MB", "100"))          # if the disk has less free space than this, all downloads are deleted
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

# Memory/CPU guards for small instances (Render free = 512 MB RAM): one download at a time, few parallel fragments,
# single-threaded ffmpeg, and a V8 heap cap for the PO-token provider.
MEDIA_CONCURRENCY = max(1, int(os.getenv("MEDIA_CONCURRENCY", "1")))
YTDLP_FRAGMENTS = max(1, int(os.getenv("YTDLP_FRAGMENTS", "2")))
POT_MAX_HEAP_MB = int(os.getenv("POT_MAX_HEAP_MB", "160"))
MEDIA_QUEUE_WAIT = float(os.getenv("MEDIA_QUEUE_WAIT", "30"))   # max seconds a download request waits for a free slot before a 503
BROWSER_CONCURRENCY = max(1, int(os.getenv("BROWSER_CONCURRENCY", "2")))  # parallel Chromium pages (screenshots)
CPU_CONCURRENCY = max(1, int(os.getenv("CPU_CONCURRENCY", "2")))         # parallel image-processing jobs
