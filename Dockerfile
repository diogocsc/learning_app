# ========= 1) Base image: slim, official Python =========
FROM python:3.11-slim AS base

# Ensure Python output is unbuffered (good for logs)
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system deps needed for some Python packages (e.g., for PDFs, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    libjpeg-dev \
    zlib1g-dev \
    libmagic1 \
 && rm -rf /var/lib/apt/lists/*

# ========= 2) Create app user & directories =========
WORKDIR /app

# Create appuser with uid 1000 IF it doesn't already exist
RUN id -u appuser >/dev/null 2>&1 || useradd -m -u 1000 appuser

# Ensure /app and /app/data exist and are owned by appuser
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# ========= 3) Install Python deps (cached layer) =========
# Copy only requirements first to leverage Docker layer caching
COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

# ========= 4) Copy app code =========
COPY . /app

# Fix ownership so non-root user can access everything
RUN chown -R appuser:appuser /app

# Now drop privileges: all subsequent commands & the container runtime
# will run as appuser
USER appuser

# ========= 5) Flask config via env vars =========
# You should set these at runtime:
# - FLASK_SECRET_KEY
# - OLLAMA_API_KEY
# Optional:
# - MAX_UPLOAD_BYTES
ENV PYTHONUNBUFFERED=1

# Expose internal port
EXPOSE 5000

# ========= 6) Start Flask =========
CMD ["python", "flask_app.py"]
