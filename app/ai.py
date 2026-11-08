"""Provider-agnostic text completion with automatic fallback.
Order: AI_PROVIDER (if set) -> Groq (fastest) -> Gemini -> OpenAI -> Anthropic -> Pollinations (keyless, rate limited).
If a provider errors or times out, the next configured one is tried, so one bad key does not break every AI endpoint."""
import hashlib

import httpx
from fastapi import HTTPException

from . import config
from .cache import TTLCache
from .http import client

_CACHE_TTL = 600
_cache = TTLCache(maxsize=500, ttl=_CACHE_TTL)


def _err(provider: str, r: httpx.Response) -> HTTPException:
    return HTTPException(502, f"AI provider error ({provider} {r.status_code}): {r.text[:250]}")


def _model(provider: str, default: str) -> str:
    """AI_MODEL only applies to the provider it was meant for, so a fallback never receives a foreign model name."""
    if config.AI_MODEL:
        configured = [n for n, k in _KEYS.items() if k()]
        if config.AI_PROVIDER == provider or (not config.AI_PROVIDER and configured == [provider]):
            return config.AI_MODEL
    return default


_KEYS = {
    "groq": lambda: config.GROQ_API_KEY,
    "gemini": lambda: config.GEMINI_API_KEY,
    "openai": lambda: config.OPENAI_API_KEY,
    "anthropic": lambda: config.ANTHROPIC_API_KEY,
}


async def _openai_style(name: str, url: str, key: str, model: str, prompt, system, max_tokens, temperature) -> dict:
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature}
    r = await client().post(url, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=60)
    if r.status_code >= 400 and name == "openai" and "max_tokens" in r.text:  # newer OpenAI models
        body["max_completion_tokens"] = body.pop("max_tokens")
        r = await client().post(url, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=60)
    if r.status_code >= 400:
        raise _err(name, r)
    return {"text": (r.json()["choices"][0]["message"].get("content") or "").strip(), "model": model, "provider": name}


async def _groq(prompt, system, mt, temp):
    return await _openai_style("groq", "https://api.groq.com/openai/v1/chat/completions", config.GROQ_API_KEY,
                               _model("groq", "llama-3.3-70b-versatile"), prompt, system, mt, temp)


async def _openai(prompt, system, mt, temp):
    return await _openai_style("openai", "https://api.openai.com/v1/chat/completions", config.OPENAI_API_KEY,
                               _model("openai", "gpt-4o-mini"), prompt, system, mt, temp)


# Google retires models for new keys without notice ("no longer available to new users"), and busy ones answer 429/503.
# So Gemini walks a list: the model from AI_MODEL (when Gemini is the intended provider) first, then current ones, then
# older ones for keys that still have them. A model that was refused is remembered for an hour and skipped.
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.1-flash-lite", "gemini-flash-latest", "gemini-3.5-flash", "gemini-2.5-flash"]
_gemini_skip: dict[str, float] = {}


def _gemini_candidates() -> list[str]:
    import time
    now = time.monotonic()
    first = _model("gemini", "")
    models = ([first] if first else []) + [m for m in GEMINI_MODELS if m != first]
    live = [m for m in models if _gemini_skip.get(m, 0) <= now]
    return live or models


async def _gemini(prompt, system, mt, temp):
    import time
    last: HTTPException | None = None
    for model in _gemini_candidates():
        gen = {"maxOutputTokens": mt, "temperature": temp}
        if "2.5" in model and "pro" not in model:
            gen["thinkingConfig"] = {"thinkingBudget": 0}  # otherwise "thinking" eats the token budget and the reply is empty
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        try:
            r = await client().post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                    headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body, timeout=15)
        except httpx.HTTPError as e:  # a stuck model must not eat the whole request: try the next one
            last = HTTPException(504, f"gemini {model} unreachable ({type(e).__name__})")
            _gemini_skip[model] = time.monotonic() + 300  # overloaded: leave it alone for 5 minutes
            continue
        if r.status_code >= 400:
            last = _err("gemini", r)
            if r.status_code == 404 or "no longer available" in r.text:
                _gemini_skip[model] = time.monotonic() + 3600
            if r.status_code in (429, 500, 503):
                _gemini_skip[model] = time.monotonic() + 300
            if r.status_code in (400, 401, 403) and "no longer available" not in r.text:
                raise last  # bad key / bad request: another model will not help
            continue  # 404 retired, 429 / 503 busy: next model
        parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if text:
            return {"text": text, "model": model, "provider": "gemini"}
        last = HTTPException(502, f"gemini {model} returned an empty reply")
    raise last or HTTPException(502, "No Gemini model answered")


async def _anthropic(prompt, system, mt, temp):
    model = _model("anthropic", "claude-sonnet-4-5")
    body = {"model": model, "max_tokens": mt, "temperature": temp, "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    r = await client().post("https://api.anthropic.com/v1/messages",
                            headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                            json=body, timeout=60)
    if r.status_code >= 400:
        raise _err("anthropic", r)
    return {"text": "".join(b.get("text", "") for b in r.json()["content"]).strip(), "model": model, "provider": "anthropic"}


async def _pollinations(prompt, system, mt, temp):
    headers = {"Authorization": f"Bearer {config.POLLINATIONS_KEY}"} if config.POLLINATIONS_KEY else {}
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    r = await client().post("https://text.pollinations.ai/openai", headers=headers, timeout=60,
                            json={"model": "openai", "messages": msgs, "max_tokens": mt, "temperature": temp})
    if r.status_code == 402:
        raise HTTPException(429, "Keyless AI tier is rate limited (1 request / 45 s). Add a free GROQ_API_KEY or "
                                 "GEMINI_API_KEY in Render > Environment for fast, unlimited-ish AI.")
    if r.status_code >= 400:
        raise _err("pollinations", r)
    return {"text": (r.json()["choices"][0]["message"].get("content") or "").strip(), "model": "openai", "provider": "pollinations"}


_IMPL = {"groq": _groq, "gemini": _gemini, "openai": _openai, "anthropic": _anthropic}


def _chain() -> list[str]:
    order = ["groq", "gemini", "openai", "anthropic"]
    if config.AI_PROVIDER in order:
        order.remove(config.AI_PROVIDER)
        order.insert(0, config.AI_PROVIDER)
    chain = [n for n in order if _KEYS[n]()]
    return chain + ["pollinations"]


async def complete(prompt: str, system: str = "", max_tokens: int = 1024, temperature: float = 0.4) -> dict:
    ck = None
    if temperature <= 0.2:  # near-deterministic requests (translate, classify...) are safe to cache
        ck = hashlib.sha1(f"{prompt}\0{system}\0{max_tokens}\0{temperature}".encode()).hexdigest()
        hit = _cache.get(ck)
        if hit is not None:
            return {**hit, "cached": True}
    last: HTTPException | None = None
    for name in _chain():
        fn = _IMPL.get(name, _pollinations)
        try:
            out = await fn(prompt, system, max_tokens, temperature)
        except HTTPException as e:
            last = e
            continue
        except httpx.HTTPError as e:
            last = HTTPException(504, f"AI provider {name} unreachable ({type(e).__name__})")
            continue
        if not out["text"]:
            last = HTTPException(502, f"AI provider {name} returned an empty reply")
            continue
        if ck:
            _cache.set(ck, out)
        return out
    raise last or HTTPException(503, "No AI provider available")
