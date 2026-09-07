import hashlib
import hmac


import httpx
from fastapi import FastAPI, HTTPException, Request
from prometheus_fastapi_instrumentator import Instrumentator

from models import Settings

settings = Settings()
app = FastAPI()
Instrumentator().instrument(app).expose(app)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(request: Request):
    body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256", "") # Reads GitHub signature

    expected = (
        "sha256="
        + hmac.new(
            settings.github_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    # computes own signature

    if not hmac.compare_digest(expected, signature_header):  # compare signatures with Github so that system avoids fake requests
        raise HTTPException(status_code=401, detail="Invalid signature")

    # forwarding the webhook to another internal service docker / kubernetes
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://webhook:8001/events",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

    return {"status": "ok"}