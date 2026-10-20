# Stage 1: Official standalone Deno binary
FROM denoland/deno:bin AS deno-bin

# Stage 2: Application container
FROM python:3.11-slim

# Prevent Python from writing bytecode and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Install system dependencies: FFmpeg and ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install Deno runtime (version 2.3+) from official image into PATH
COPY --from=deno-bin /deno /usr/local/bin/deno
RUN chmod +x /usr/local/bin/deno && deno --version

# Install Python dependencies including yt-dlp[default] and yt-dlp-ejs
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Ensure runtime directories exist
RUN mkdir -p downloads

# Respect platform-provided PORT (Render default is $PORT)
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]

