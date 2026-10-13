# ---------------------------------------------------------------------------
# Stage 1: Prebuilt Deno and PO Token Provider Assets
# ---------------------------------------------------------------------------
FROM denoland/deno:bin AS deno_stage
FROM brainicism/bgutil-ytdlp-pot-provider:latest AS pot_provider_stage

# ---------------------------------------------------------------------------
# Stage 2: Main Production Application Container
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install FFmpeg, curl, and CA certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install Deno JavaScript runtime from official image
COPY --from=deno_stage /deno /usr/local/bin/deno

# Install Node.js >= 22 runtime and local POT Provider from official bgutil image
COPY --from=pot_provider_stage /usr/local/bin/node /usr/local/bin/node
COPY --from=pot_provider_stage /app /opt/pot-provider

# Copy requirements and install Python dependencies including yt-dlp-ejs and bgutil plugin
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure downloads directory exists
RUN mkdir -p downloads

# Informational port exposure
EXPOSE 8000

# Execute Uvicorn binding to 0.0.0.0 using dynamic platform PORT (fallback 8000)
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
