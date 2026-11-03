"""Developer & text utilities: encoders, formatters, converters and generators that need no external service.
Everything here is pure Python (stdlib only), so it works offline and answers instantly.
All routes are under /v1/dev and require the usual API key."""
import base64
import binascii
import colorsys
import difflib
import html
import json
import random
import re
import secrets
import string
import time
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/dev", tags=["dev"])


class TextReq(BaseModel):
    text: str = Field(min_length=1, max_length=200000)


# ------------------------------------------------------------------ encoders ----
@router.post("/base64/encode")
def b64_encode(r: TextReq, urlsafe: bool = False):
    """Base64-encode UTF-8 text. urlsafe=true uses the URL-safe alphabet (- and _)."""
    raw = r.text.encode()
    out = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(raw).decode()
    return {"input_bytes": len(raw), "encoding": "base64url" if urlsafe else "base64", "result": out}


@router.post("/base64/decode")
def b64_decode(r: TextReq, urlsafe: bool = False):
    """Decode Base64 text back to UTF-8. Tolerates missing padding and whitespace, rejects invalid characters."""
    s = re.sub(r"\s+", "", r.text)
    if urlsafe:
        s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        raw = base64.b64decode(s, validate=True)
        return {"decoded_bytes": len(raw), "result": raw.decode("utf-8", "replace")}
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Invalid Base64 input")


@router.post("/url/encode")
def url_encode(r: TextReq, component: bool = True):
    """Percent-encode text. component=true encodes a query value; false keeps / and : (whole URL)."""
    return {"result": quote(r.text, safe="" if component else "/:?=&")}


@router.post("/url/decode")
def url_decode(r: TextReq):
    return {"result": unquote(r.text)}


@router.post("/html/escape")
def html_escape(r: TextReq, quote_all: bool = True):
    return {"result": html.escape(r.text, quote=quote_all)}


@router.post("/html/unescape")
def html_unescape(r: TextReq):
    return {"result": html.unescape(r.text)}


# ------------------------------------------------------------------ json ----
@router.post("/json/format")
def json_format(r: TextReq, indent: int = Query(2, ge=0, le=8)):
    """Pretty-print (indent>0) or minify (indent=0) JSON. Returns the parsed type too."""
    try:
        obj = json.loads(r.text)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e.msg} at line {e.lineno} column {e.colno}")
    out = json.dumps(obj, indent=indent if indent else None, ensure_ascii=False,
                     separators=None if indent else (",", ":"))
    return {"valid": True, "type": type(obj).__name__, "result": out}


@router.post("/json/validate")
def json_validate(r: TextReq):
    try:
        obj = json.loads(r.text)
    except json.JSONDecodeError as e:
        return {"valid": False, "error": e.msg, "line": e.lineno, "column": e.colno}
    return {"valid": True, "type": type(obj).__name__,
            "keys": list(obj.keys())[:50] if isinstance(obj, dict) else None,
            "length": len(obj) if isinstance(obj, (dict, list)) else None}


# ------------------------------------------------------------------ text ----
_CASE = ("upper", "lower", "title", "sentence", "camel", "pascal", "snake", "kebab", "constant", "dot")


def _words(t: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", t)


@router.post("/case")
def case_convert(r: TextReq, style: Literal[_CASE] = "snake"):  # type: ignore[valid-type]
    """Convert text between common naming styles."""
    w = _words(r.text)
    low = [x.lower() for x in w]
    if style == "upper":
        out = r.text.upper()
    elif style == "lower":
        out = r.text.lower()
    elif style == "title":
        out = r.text.title()
    elif style == "sentence":
        out = re.sub(r"\s+", " ", r.text).strip()
        out = out[:1].upper() + out[1:].lower() if out else out
    elif style == "camel":
        out = (low[0] + "".join(x.capitalize() for x in low[1:])) if low else ""
    elif style == "pascal":
        out = "".join(x.capitalize() for x in low)
    elif style == "snake":
        out = "_".join(low)
    elif style == "kebab":
        out = "-".join(low)
    elif style == "constant":
        out = "_".join(low).upper()
    else:  # dot
        out = ".".join(low)
    return {"style": style, "result": out}


@router.post("/text-stats")
def text_stats(r: TextReq):
    """Character / word / line / sentence counts, reading and speaking time, top words."""
    t = r.text
    words = re.findall(r"\b[\w'-]+\b", t)
    sentences = [s for s in re.split(r"[.!?]+", t) if s.strip()]
    paragraphs = [p for p in re.split(r"\n\s*\n", t) if p.strip()]
    freq: dict[str, int] = {}
    for w in (x.lower() for x in words):
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return {
        "characters": len(t), "characters_no_spaces": len(re.sub(r"\s", "", t)),
        "words": len(words), "lines": t.count("\n") + 1, "sentences": len(sentences),
        "paragraphs": len(paragraphs),
        "avg_word_length": round(sum(len(w) for w in words) / len(words), 2) if words else 0,
        "reading_time_min": round(len(words) / 200, 1), "speaking_time_min": round(len(words) / 130, 1),
        "top_words": [{"word": w, "count": c} for w, c in top],
    }


@router.post("/diff")
def diff(r: TextReq, other: str = Query(..., max_length=200000), context: bool = True):
    """Line diff between `text` (old) and `other` (new). context=true returns unified diff text."""
    a, b = r.text.splitlines(), other.splitlines()
    if context:
        out = "\n".join(difflib.unified_diff(a, b, fromfile="old", tofile="new", lineterm=""))
        return {"changed": bool(out), "unified": out}
    ops = [{"op": op, "a": i1, "b": i2, "lines": a[i1:i2] if op != "insert" else b[i1:i2]}
           for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes() if op != "equal"]
    return {"changed": bool(ops), "changes": ops}


# ------------------------------------------------------------------ generators ----
_LOREM = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore "
          "magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris nisi aliquip ex ea commodo "
          "consequat duis aute irure in reprehenderit voluptate velit esse cillum eu fugiat nulla pariatur excepteur "
          "sint occaecat cupidatat non proident sunt culpa qui officia deserunt mollit anim id est laborum").split()


@router.get("/lorem")
def lorem(paragraphs: int = Query(1, ge=1, le=20), sentences: int = Query(4, ge=1, le=20), start_with_lorem: bool = True):
    """Placeholder text. Each paragraph has `sentences` sentences."""
    rnd = random.Random()
    out = []
    for p in range(paragraphs):
        sents = []
        for _ in range(sentences):
            n = rnd.randint(6, 14)
            ws = [rnd.choice(_LOREM) for _ in range(n)]
            sents.append(" ".join(ws).capitalize() + ".")
        para = " ".join(sents)
        if p == 0 and start_with_lorem:
            para = "Lorem ipsum dolor sit amet, " + para[0].lower() + para[1:]
        out.append(para)
    return {"paragraphs": out, "text": "\n\n".join(out)}


@router.get("/password")
def password(length: int = Query(16, ge=4, le=128), count: int = Query(1, ge=1, le=50),
             symbols: bool = True, digits: bool = True, upper: bool = True):
    """Cryptographically strong random passwords (secrets module)."""
    pool = string.ascii_lowercase
    if upper:
        pool += string.ascii_uppercase
    if digits:
        pool += string.digits
    if symbols:
        pool += "!@#$%^&*()-_=+[]{};:,.<>?"
    pwds = ["".join(secrets.choice(pool) for _ in range(length)) for _ in range(count)]
    entropy = round(length * (len(pool).bit_length()), 1)
    return {"length": length, "alphabet_size": len(pool), "approx_entropy_bits": entropy, "passwords": pwds}


@router.get("/random")
def random_value(type: Literal["number", "string", "uuid", "color", "coin", "dice", "choice"] = "number",
                 minimum: int = 1, maximum: int = 100, length: int = 12,
                 choices: str = Query("a,b,c", max_length=2000)):
    """Random value: number, string, uuid, hex color, coin flip, dice roll, or a pick from `choices`."""
    import uuid as _uuid
    if type == "number":
        if minimum > maximum:
            raise HTTPException(400, "minimum must be <= maximum")
        return {"type": type, "value": random.randint(minimum, maximum)}
    if type == "string":
        return {"type": type, "value": "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(min(length, 256)))}
    if type == "uuid":
        return {"type": type, "value": str(_uuid.uuid4())}
    if type == "color":
        return {"type": type, "value": "#%06x" % secrets.randbelow(0x1000000)}
    if type == "coin":
        return {"type": type, "value": random.choice(["heads", "tails"])}
    if type == "dice":
        return {"type": type, "value": random.randint(1, 6)}
    opts = [c.strip() for c in choices.split(",") if c.strip()]
    if not opts:
        raise HTTPException(400, "choices is empty")
    return {"type": "choice", "value": random.choice(opts)}


# ------------------------------------------------------------------ time ----
@router.get("/timestamp")
def timestamp(epoch: float | None = None):
    """Current time (or a given epoch) as epoch seconds/ms, ISO-8601 UTC, and broken-down components."""
    ts = epoch if epoch is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return {"epoch": ts, "epoch_ms": int(ts * 1000), "iso_utc": dt.isoformat().replace("+00:00", "Z"),
            "date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M:%S"),
            "weekday": dt.strftime("%A"), "week": dt.isocalendar().week, "year": dt.year}


@router.post("/timestamp/convert")
def timestamp_convert(value: str = Query(..., max_length=64)):
    """Parse an epoch (s or ms) or an ISO date string and return every common representation."""
    v = value.strip()
    ts = None
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        n = float(v)
        ts = n / 1000 if n > 1e11 else n  # treat big numbers as milliseconds
    else:
        try:
            ts = datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise HTTPException(400, "Could not parse value as epoch or ISO-8601 date")
    return timestamp(ts)


# ------------------------------------------------------------------ color ----
_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB = re.compile(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", re.I)


@router.get("/color")
def color(hex: str | None = Query(None, max_length=32), rgb: str | None = Query(None, max_length=32)):
    """Convert a color between HEX, RGB and HSL, with luminance and contrast ratios against white/black."""
    if hex:
        m = _HEX.match(hex.strip())
        if not m:
            raise HTTPException(400, "hex must be #rgb or #rrggbb")
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    elif rgb:
        m = _RGB.match(rgb.strip())
        if not m:
            raise HTTPException(400, "rgb must look like rgb(255,0,128)")
        r, g, b = (int(x) for x in m.groups())
        if any(v > 255 for v in (r, g, b)):
            raise HTTPException(400, "rgb components must be 0-255")
    else:
        raise HTTPException(400, "Provide hex= or rgb=")

    def _lum(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    L = 0.2126 * _lum(r) + 0.7152 * _lum(g) + 0.0722 * _lum(b)
    hh, ll, ss = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return {
        "hex": "#%02x%02x%02x" % (r, g, b), "rgb": {"r": r, "g": g, "b": b},
        "hsl": {"h": round(hh * 360), "s": round(ss * 100), "l": round(ll * 100)},
        "luminance": round(L, 4),
        "contrast_vs_white": round((1.05) / (L + 0.05), 2),
        "contrast_vs_black": round((L + 0.05) / 0.05, 2),
        "is_light": L > 0.179,
    }


# ------------------------------------------------------------------ misc ----
@router.get("/number-base")
def number_base(value: str = Query(..., max_length=64), from_base: int = Query(10, ge=2, le=36),
                to_base: int = Query(16, ge=2, le=36)):
    """Convert an integer between bases 2-36."""
    try:
        n = int(value.strip(), from_base)
    except ValueError:
        raise HTTPException(400, f"{value!r} is not a valid base-{from_base} integer")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    neg = n < 0
    n = abs(n)
    if n == 0:
        out = "0"
    else:
        buf = []
        while n:
            n, rem = divmod(n, to_base)
            buf.append(digits[rem])
        out = "".join(reversed(buf))
    return {"input": value, "from_base": from_base, "to_base": to_base, "result": ("-" if neg else "") + out}


@router.post("/regex")
def regex_test(r: TextReq, pattern: str = Query(..., max_length=2000),
               ignore_case: bool = False, multiline: bool = False, dotall: bool = False,
               limit: int = Query(100, ge=1, le=1000)):
    """Test a regular expression against text. Returns matches (with groups) and counts."""
    flags = 0
    if ignore_case:
        flags |= re.I
    if multiline:
        flags |= re.M
    if dotall:
        flags |= re.S
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        raise HTTPException(400, f"Invalid regex: {e}")
    matches = []
    for m in rx.finditer(r.text):
        matches.append({"match": m.group(0), "start": m.start(), "end": m.end(),
                        "groups": list(m.groups())})
        if len(matches) >= limit:
            break
    return {"pattern": pattern, "valid": True, "count": len(matches), "matches": matches}


@router.get("/jwt/decode")
def jwt_decode(token: str = Query(..., max_length=8000)):
    """Decode a JWT's header and payload WITHOUT verifying the signature. Never trust the output for auth."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise HTTPException(400, "A JWT must have three dot-separated parts")
    def _seg(seg: str):
        seg += "=" * (-len(seg) % 4)
        try:
            return json.loads(base64.urlsafe_b64decode(seg))
        except Exception:
            raise HTTPException(400, "Token parts are not valid Base64URL JSON")
    header, payload = _seg(parts[0]), _seg(parts[1])
    exp = payload.get("exp") if isinstance(payload, dict) else None
    return {"header": header, "payload": payload, "signature_present": bool(parts[2]),
            "expired": bool(exp and exp < time.time()),
            "expires_iso": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None,
            "note": "Signature is NOT verified."}
