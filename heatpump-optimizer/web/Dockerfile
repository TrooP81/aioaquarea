ARG APP_VERSION=0.15.0
ARG BUILD_REVISION=unknown

# node:20-alpine, pinned by the official multi-architecture digest.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS builder
WORKDIR /app
COPY package.json package-lock.json ./
COPY scripts/patch-next-zod.mjs ./scripts/patch-next-zod.mjs
COPY patches/next-zod.json ./patches/next-zod.json
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
COPY --from=builder --chown=appuser:appgroup /app/.next/standalone ./
COPY --from=builder --chown=appuser:appgroup /app/.next/static ./.next/static
COPY --from=builder --chown=appuser:appgroup /app/scripts/patch-next-zod.mjs ./scripts/patch-next-zod.mjs
COPY --from=builder --chown=appuser:appgroup /app/patches/next-zod.json ./patches/next-zod.json

ARG APP_VERSION
ARG BUILD_REVISION
LABEL org.opencontainers.image.version="${APP_VERSION}" \
	org.opencontainers.image.revision="${BUILD_REVISION}" \
	org.opencontainers.image.source="https://github.com/TrooP81/aioaquarea"
ENV APP_VERSION="${APP_VERSION}" \
	BUILD_REVISION="${BUILD_REVISION}"

USER appuser
# Mandatory final-stage verification after all artifact copies, as the runner user.
# CMD does not patch or fetch anything; only this verified artifact is launchable.
RUN node scripts/patch-next-zod.mjs verify
EXPOSE 3000
CMD ["node", "server.js"]
