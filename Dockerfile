FROM node:22-bookworm-slim AS web-build

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build && npm prune --omit=dev


FROM python:3.12-slim AS runtime

ARG SOVEREIGN_VERSION=dev

LABEL maintainer="BrewtaniusAI" \
      description="Sovereign Claw — Governed Sovereign Agent Runtime" \
      version="${SOVEREIGN_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOVEREIGN_BRIDGE_HOST=0.0.0.0 \
    SOVEREIGN_BRIDGE_PORT=8787 \
    SOVEREIGN_PYTHON=python3

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl git && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .

COPY --from=web-build /usr/local/bin/node /usr/local/bin/node
COPY web/server.js web/package.json /app/web/
COPY --from=web-build /web/node_modules /app/web/node_modules
COPY --from=web-build /web/dist /app/web/dist

RUN useradd -m -r -d /home/sovereign sovereign && \
    mkdir -p /app/data /home/sovereign/.sovereign_claw/skills && \
    chown -R sovereign:sovereign /app /home/sovereign

USER sovereign

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD curl -fsS http://127.0.0.1:8787/ready || exit 1

ENTRYPOINT ["node", "web/server.js"]
