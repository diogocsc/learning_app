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

# Create streamlituser with uid 1000 IF it doesn't already exist
RUN id -u streamlituser >/dev/null 2>&1 || useradd -m -u 1000 streamlituser

# Ensure /app and /app/data exist and are owned by streamlituser
RUN mkdir -p /app/data && chown -R streamlituser:streamlituser /app

# ========= 3) Install Python deps (cached layer) =========
# Copy only requirements first to leverage Docker layer caching
COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

# ========= 4) Copy app code =========
COPY . /app

# Fix ownership so non-root user can access everything
RUN chown -R streamlituser:streamlituser /app

# Now drop privileges: all subsequent commands & the container runtime
# will run as streamlituser
USER streamlituser

# ========= 5) Streamlit config via env vars =========
ENV STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Expose internal port
EXPOSE 8501

# ========= 6) Start Streamlit =========
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
