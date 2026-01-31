# WX Sniper v4.6 - Docker image for AWS ECS/Fargate
# Build: docker build -t wx-sniper:latest .
# Run:   docker run -e LIVE_MODE=false -e MAX_PRICE=95 -p 8080:8080 wx-sniper:latest

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY smart_poller_v46_aws.py ./smart_poller.py

# Create non-root user for security
RUN useradd -m -u 1000 sniper && chown -R sniper:sniper /app
USER sniper

# Environment defaults
ENV PORT=8080
ENV LIVE_MODE=false
ENV MAX_PRICE=95
ENV AWS_REGION=us-east-1
ENV PYTHONUNBUFFERED=1

# Health check for ECS/ALB
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Expose dashboard port
EXPOSE 8080

# Run the sniper
CMD ["python", "smart_poller.py"]
