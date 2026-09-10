FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-downloads the embedding model into the image at build time (name
# must match grocery_agent/embeddings.py's MODEL_NAME) — Fly stops idle
# machines and restarts them on the next request (see fly.toml), so
# without this every cold start would otherwise fetch it live over the
# network before it could serve anything. Placed before COPY grocery_agent
# so a code-only change doesn't invalidate this layer and force a re-download.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

COPY grocery_agent ./grocery_agent

EXPOSE 8080

CMD ["uvicorn", "grocery_agent.api:app", "--host", "0.0.0.0", "--port", "8080"]
