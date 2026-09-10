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
# (autornd dstack-nebius-patterns skill §6.1 + §6.2)
RUN python - <<'PYEOF'
import pathlib
site = next((pathlib.Path("/dstack-server/.venv/lib")).glob("python3.*/site-packages/gpuhunt"))
constraints = site / "_internal" / "constraints.py"
text = constraints.read_text()
anchor = 'NvidiaGPUInfo(name="RTX6000", memory=24, compute_capability=(7, 5)),'
assert anchor in text, "gpuhunt constraints anchor missing"
if "RTXPRO6000" not in text:
    added = anchor + '\n    NvidiaGPUInfo(name="RTXPRO6000", memory=96, compute_capability=(12, 0)),'
    constraints.write_text(text.replace(anchor, added, 1))
nebius = site / "providers" / "nebius.py"
text = nebius.read_text()
old_re = 'm = re.match(r"gpu-([^-]+)-", platform)'
assert old_re in text, "gpuhunt nebius regex anchor missing"
text = text.replace(old_re, 'm = re.match(r"gpu-([^-]+)", platform)', 1)
old_grp = "    gpu_name = m.group(1)\n"
assert old_grp in text, "gpuhunt nebius gpu_name anchor missing"
text = text.replace(
    old_grp,
    '    gpu_name = m.group(1)\n    if gpu_name == "rtx6000":\n        gpu_name = "RTXPRO6000"\n',
    1,
)
nebius.write_text(text)
assert "RTXPRO6000" in constraints.read_text(), "constraints verify failed"
assert 'r"gpu-([^-]+)"' in nebius.read_text(), "nebius regex verify failed"
print("[OK] gpuhunt rtx6000 patch applied at", site)
PYEOF

# Add virtualenv to PATH so entrypoint can find dstack
ENV PATH="/dstack-server/.venv/bin:$PATH"

COPY docker/server/entrypoint.sh entrypoint.sh
RUN chmod 777 entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--", "./entrypoint.sh"]
