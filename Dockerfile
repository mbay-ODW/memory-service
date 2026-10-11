FROM python:3.12-slim
# Docker-Secrets: NAME_FILE=/run/secrets/... wird beim Start zu NAME geladen
COPY --chmod=0755 env-secrets.sh /usr/local/bin/env-secrets

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/data/hf-cache

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/env-secrets"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
