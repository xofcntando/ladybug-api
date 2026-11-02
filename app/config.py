import os
from pathlib import Path

API_KEYS = {k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()}
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "./storage"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_MB", "25")) * 1024 * 1024
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
