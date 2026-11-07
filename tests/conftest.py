import os
import tempfile

os.environ.setdefault("API_KEYS", "testkey")
os.environ.setdefault("STORAGE_DIR", tempfile.mkdtemp(prefix="lb_test_"))
os.environ.setdefault("SELF_PING", "0")
