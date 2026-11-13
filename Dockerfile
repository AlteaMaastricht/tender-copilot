FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY pyproject.toml CLAUDE.md ./
COPY ingest/ ingest/
COPY rag/ rag/
COPY llm/ llm/
COPY pipeline/ pipeline/
COPY agent/ agent/
COPY jobs/ jobs/
COPY app/ app/
COPY crm/ crm/
COPY evals/ evals/
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
