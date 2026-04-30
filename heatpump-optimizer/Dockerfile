FROM python:3.12-slim AS base

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

CMD ["python", "-m", "packages.poller.main"]
