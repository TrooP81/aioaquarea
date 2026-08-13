# python:3.12.13-slim-trixie, pinned by the official multi-architecture digest.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS base

WORKDIR /app

# Install system deps for asyncpg, scientific libs, and git (for VCS deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev git && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[all]"

COPY . .

# Install the project itself
RUN pip install --no-cache-dir -e .

RUN useradd --create-home --shell /bin/bash appuser \
 && mkdir -p /app/models \
 && chown -R appuser:appuser /app/models
USER appuser

CMD ["python", "-m", "packages.poller.main"]
