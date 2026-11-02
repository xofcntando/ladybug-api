# Ladybug API

FastAPI service with file, download and AI endpoints. Auth: `X-API-Key` header on all `/v1` routes. Interactive docs at `/docs`.

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | /health | Health check (no auth) |
| POST | /v1/files | Upload file (multipart `file`) |
| GET | /v1/files, /v1/files/{id} | List / info |
| GET | /v1/files/{id}/download | Download stored file |
| DELETE | /v1/files/{id} | Delete |
| POST | /v1/download/fetch `{url}` | Fetch remote URL server-side and store it |
| GET | /v1/download/proxy?url= | Stream a remote file back |
| POST | /v1/export/csv `{rows}` | JSON rows to CSV download |
| POST | /v1/ai/chat, summarize, translate, product-description, classify | AI endpoints |

## Deploy on Render
1. Push this folder to a GitHub repo.
2. Render dashboard: New > Blueprint > pick the repo (it reads `render.yaml`).
3. Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. `API_KEYS` is auto-generated (Environment tab); comma-separate to add more.
4. Free plan disk is ephemeral: stored files vanish on redeploy/restart. For permanent storage, use a paid plan and add to the service:
   `disk: {name: files, mountPath: /var/data, sizeGB: 1}` and set `STORAGE_DIR=/var/data`.

## Local run
    pip install -r requirements.txt
    API_KEYS=test uvicorn app.main:app --reload

## Example
    curl -H "X-API-Key: KEY" -H "Content-Type: application/json" \
      -d '{"text":"Hello","target_language":"Hungarian"}' https://YOUR-SERVICE.onrender.com/v1/ai/translate
