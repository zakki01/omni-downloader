# ---------------------------------------------------------------------------
# Stage 1: Official Prebuilt Deno JavaScript Runtime
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

# Install system dependencies: FFmpeg, curl, CA certs, Node.js 22 LTS, and canvas graphics libraries
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends \
        nodejs \
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libpixman-1-0 \
        libgif7 \
        librsvg2-2 && \
    rm -rf /var/lib/apt/lists/*

# Install Deno JavaScript runtime from official image
COPY --from=deno_stage /deno /usr/local/bin/deno

# Copy prebuilt BGUtil POT Provider from official image and local fallback
COPY --from=pot_provider_stage /app /opt/pot-provider

# Copy requirements and install Python dependencies including yt-dlp-ejs, bgutil plugin, curl-cffi
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files (including local pot-provider and start.sh)
COPY . .

# Ensure downloads and logs directories exist and start script is executable
RUN mkdir -p downloads logs && \
    chmod +x /app/start.sh

# Informational port exposure (Only app port, port 4416 is internal only!)
EXPOSE 8000

# Execute container supervisor script
CMD ["/app/start.sh"]
