import os
from pathlib import Path

API_KEYS = {k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()}
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "./storage"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_MB", "25")) * 1024 * 1024
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

MEDIA_MAX_MB = int(os.getenv("MEDIA_MAX_MB", "100"))
MEDIA_MAX_MINUTES = int(os.getenv("MEDIA_MAX_MINUTES", "20"))
MEDIA_TTL_MINUTES = int(os.getenv("MEDIA_TTL_MINUTES", "60"))
YT_COOKIES = os.getenv("YT_COOKIES", "")  # optional: Netscape-format cookies text, helps with YouTube bot checks

IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
TTS_MODEL = os.getenv("TTS_MODEL", "tts-1")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

YT_PROXY = os.getenv("YT_PROXY", "")  # optional: http(s)/socks5 proxy URL, e.g. a residential proxy
