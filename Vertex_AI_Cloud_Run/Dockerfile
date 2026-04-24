# ==============================================================
# Cloud Run Job Dockerfile for bank_ocr_cloudrun_worker
# ==============================================================

# ---- 1. Base image ----
# Use Python 3.11 slim for smaller image size & better performance
FROM python:3.11-slim

# ---- 2. Environment settings ----
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# ---- 3. Set working directory ----
WORKDIR /app

# ---- 4. System dependencies ----
# Includes headers for Pillow, PyPDF, and Google libs
# Note: No poppler needed - using Gemini native PDF support
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    libtiff5-dev \
    libopenjp2-7-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---- 5. Copy and install Python dependencies ----
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# ---- 6. Copy application code ----
COPY . .

# ---- 7. Run as non-root user ----
RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

# ---- 8. Default entrypoint ----
ENTRYPOINT ["python", "main.py"]
