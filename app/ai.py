import httpx
from fastapi import HTTPException

from . import config


async def complete(prompt: str, system: str = "", max_tokens: int = 1024, temperature: float = 0.4) -> dict:
    """Provider-agnostic text completion. First key found wins: Anthropic, OpenAI, Gemini, Groq."""
    async with httpx.AsyncClient(timeout=90) as c:
        if config.ANTHROPIC_API_KEY:
            model = config.AI_MODEL or "claude-sonnet-4-5"
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={"model": model, "max_tokens": max_tokens, "temperature": temperature,
                      "system": system, "messages": [{"role": "user", "content": prompt}]},
            )
            if r.status_code >= 400:
                raise HTTPException(502, f"AI provider error: {r.text[:300]}")
            d = r.json()
            return {"text": "".join(b.get("text", "") for b in d["content"]), "model": model, "provider": "anthropic"}
        if config.OPENAI_API_KEY:
            model = config.AI_MODEL or "gpt-4o-mini"
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                json={"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature},
            )
            if r.status_code >= 400:
                raise HTTPException(502, f"AI provider error: {r.text[:300]}")
            return {"text": r.json()["choices"][0]["message"]["content"], "model": model, "provider": "openai"}
        if config.GEMINI_API_KEY:
            model = config.AI_MODEL or "gemini-2.5-flash"
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                             headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body)
            if r.status_code >= 400:
                raise HTTPException(502, f"AI provider error: {r.text[:300]}")
            parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
            return {"text": "".join(p.get("text", "") for p in parts), "model": model, "provider": "gemini"}
        if config.GROQ_API_KEY:
            model = config.AI_MODEL or "llama-3.3-70b-versatile"
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                             headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                             json={"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature})
            if r.status_code >= 400:
                raise HTTPException(502, f"AI provider error: {r.text[:300]}")
            return {"text": r.json()["choices"][0]["message"]["content"], "model": model, "provider": "groq"}
    raise HTTPException(503, "No AI provider configured. In Render > Environment add GEMINI_API_KEY (free), GROQ_API_KEY (free), ANTHROPIC_API_KEY or OPENAI_API_KEY.")
