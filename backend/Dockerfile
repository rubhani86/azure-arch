FROM python:3.11-slim

# System basics
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for better caching
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY backend/ /app/

# Defaults (can be overridden by env / compose)
ENV PYTHONUNBUFFERED=1 \
    MONGO_DB_NAME=azure_arch_db \
    MONGO_COLL_NAME=architectures

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
