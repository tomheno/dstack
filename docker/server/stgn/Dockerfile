# Stage 1: Build frontend
FROM node:20-slim AS frontend-builder

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python server
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV DSTACK_SERVER_LOG_FORMAT=json

WORKDIR /dstack-server

RUN apt-get update && apt-get install -y \
    curl \
    git \
    sqlite3 \
    tini \
    && rm -rf /var/lib/apt/lists/*

RUN if [ $(uname -m) = "aarch64" ]; then ARCH="arm64"; else ARCH="x86_64"; fi && \
    curl https://github.com/benbjohnson/litestream/releases/download/v0.5.0/litestream-0.5.0-linux-$ARCH.deb -O -L && \
    dpkg -i litestream-0.5.0-linux-$ARCH.deb

ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin/:$PATH"

COPY pyproject.toml uv.lock README.md ./
COPY src src
COPY examples examples

# Copy built frontend to statics directory
COPY --from=frontend-builder /frontend/build src/dstack/_internal/server/statics

RUN uv sync --extra all

# Patch gpuhunt for Nebius RTX PRO 6000 (gpu-rtx6000, sm_120, 96GB). The offline
# catalog lacks the GPU; register it in the constraints table and fix the
# platform-name regex that drops the trailing-less "gpu-rtx6000" and mis-maps it
# to the 24GB Turing RTX6000. Pairs with the compute.py live-offer injection.
# A COPYed script (not a RUN heredoc) so it also works on the legacy Docker
# builder. (autornd dstack-nebius-patterns skill §6.1 + §6.2)
COPY docker/server/stgn/patch_gpuhunt.py /tmp/patch_gpuhunt.py
RUN python /tmp/patch_gpuhunt.py

# Add virtualenv to PATH so entrypoint can find dstack
ENV PATH="/dstack-server/.venv/bin:$PATH"

COPY docker/server/entrypoint.sh entrypoint.sh
RUN chmod 777 entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--", "./entrypoint.sh"]
