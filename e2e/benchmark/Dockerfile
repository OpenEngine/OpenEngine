# The computer the benchmark's journey happens on: a clean Linux machine with what a person would have installed
# before starting (git, Node, the GitHub CLI, a text editor, the Cloudflare tunnel program and the coding agents)
# and nothing of OpenEngine. The journey installs that. Built once by `npm run oe-benchmark` and kept in Docker's cache.
FROM node:26-bookworm

ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends vim procps less \
 && mkdir -p -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${TARGETARCH}" \
 && chmod +x /usr/local/bin/cloudflared

# The OpenEngine installer from a checkout (scripts/install-local.sh) needs uv, the same version install.sh pins.
COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv

RUN npm install -g @anthropic-ai/claude-code @openai/codex

USER node
ENV HOME=/home/node
ENV PATH=/home/node/.local/bin:$PATH
WORKDIR /home/node
CMD ["sleep", "infinity"]
