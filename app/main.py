"""FastAPI service. For now it only exposes a health check."""

from fastapi import FastAPI

app = FastAPI(title="Tender Copilot")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
