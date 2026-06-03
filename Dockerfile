FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev

# Copy application code
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Non-root user for runtime
RUN useradd -r -s /bin/false appuser \
    && mkdir -p /app/backend/cache \
    && chown -R appuser:appuser /app/backend/cache
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]

CMD ["uv", "run", "--directory", "/app/backend", "--no-sync", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
