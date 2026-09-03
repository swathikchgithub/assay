FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY assay ./assay
COPY demo ./demo
COPY deploy ./deploy

RUN pip install --no-cache-dir ".[nlq,service]"

ENV ASSAY_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn deploy.railway.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
