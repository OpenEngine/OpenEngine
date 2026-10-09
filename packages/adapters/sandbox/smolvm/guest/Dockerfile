# Build from the repository root for linux/arm64 or linux/amd64.
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/ /usr/local/
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/openengine
COPY packages/adapters/sandbox/smolvm/guest/package*.json ./
RUN npm ci --omit=dev \
    && npm cache clean --force \
    && node --version && git --version \
    && test -x node_modules/.bin/codex-acp \
    && test -x node_modules/.bin/claude-agent-acp
ENV PATH="/opt/openengine/node_modules/.bin:${PATH}"
RUN mkdir -p /workspace
WORKDIR /workspace
CMD ["sleep", "infinity"]
