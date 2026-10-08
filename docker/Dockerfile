# syntax=docker/dockerfile:1
#
# agent-boundary-bench on Linux: Python + Node 24 + the npm agents pinned in agents.lock.json.
# Build from the repo root (the build context must be the repo, not docker/):
#
#   docker build -f docker/Dockerfile -t agent-boundary-bench --build-arg GIT_COMMIT=$(git rev-parse HEAD) .
#
# No API key is baked in. Pass keys at run time, e.g. `docker run -e DEEPSEEK_API_KEY ...`.
# Hermes Agent is not installed (see docker/README.md).

ARG NODE_VERSION=24.21.0
ARG PYTHON_VERSION=3.14

# Node comes from the official image; only its binary and bundled npm are copied.
FROM node:${NODE_VERSION}-bookworm-slim AS node

FROM python:${PYTHON_VERSION}-slim-bookworm

ARG GIT_COMMIT=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version && python --version

# Unprivileged user; agents run as this user, never as root.
RUN useradd --create-home --uid 10001 bench \
 && mkdir -p /opt/agent-boundary-bench /tmp/abx \
 && chown bench:bench /opt/agent-boundary-bench /tmp/abx

# bench/config.py contract. ABB_NODE_DIR points at the image's Node (>= 24.16, what
# OpenClaw needs); Hermes is not in the image, so ABB_HERMES_EXE is left unset.
ENV ABB_SANDBOX_ROOT=/tmp/abx \
    ABB_AGENTS_DIR=/opt/agent-boundary-bench/agents \
    ABB_NODE_DIR=/usr/local/bin \
    ABB_GIT_COMMIT=${GIT_COMMIT} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    npm_config_update_notifier=false

USER bench
WORKDIR /opt/agent-boundary-bench

# Agents first, so the (slow) npm layer is cached until the lockfile changes.
# setup.sh runs `npm ci` from scripts/npm/<agent>/package-lock.json (exact transitive tree).
COPY --chown=bench:bench agents.lock.json ./
COPY --chown=bench:bench scripts/ ./scripts/
RUN bash scripts/setup.sh \
 && test -x agents/openclaw/node_modules/.bin/openclaw \
 && test -x agents/dsh/node_modules/.bin/dsh \
 && npm cache clean --force

# The harness and the gateway (results/, site/, agents/ and secrets are excluded by
# docker/Dockerfile.dockerignore).
COPY --chown=bench:bench bench/ ./bench/
COPY --chown=bench:bench gateway/ ./gateway/

# Default: the model-free gateway checks (no key, no network needed).
CMD ["python", "gateway/test_gateway.py"]
