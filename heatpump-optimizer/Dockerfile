ARG APP_VERSION=0.13.3
ARG BUILD_REVISION=unknown

# python:3.12.13-slim-trixie, pinned by the official multi-architecture digest.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS base

WORKDIR /app

# Install system deps for asyncpg, scientific libs, and git (for VCS deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev git && \
    rm -rf /var/lib/apt/lists/*

COPY heatpump-optimizer/pyproject.toml ./
RUN pip install --no-cache-dir -e ".[all]"

COPY heatpump-optimizer/ .

# The optimizer and aioaquarea live in the same repository. Install the local
# library after third-party dependencies so production images contain the
# exact Panasonic integration that was reviewed and merged with the app.
COPY pyproject.toml README.md LICENSE /opt/aioaquarea-src/
COPY aioaquarea/ /opt/aioaquarea-src/aioaquarea/

# Install the project itself
RUN pip install --no-cache-dir --force-reinstall --no-deps /opt/aioaquarea-src \
 && pip install --no-cache-dir --no-deps -e .

RUN useradd --create-home --shell /bin/bash appuser \
 && mkdir -p /app/models \
 && chown -R appuser:appuser /app/models

ARG APP_VERSION
ARG BUILD_REVISION
LABEL org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${BUILD_REVISION}" \
      org.opencontainers.image.source="https://github.com/TrooP81/aioaquarea"
ENV APP_VERSION="${APP_VERSION}" \
    BUILD_REVISION="${BUILD_REVISION}"

USER appuser

CMD ["python", "-m", "packages.poller.main"]
