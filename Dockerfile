# ==============================================================================
# /opt/ssdlc/Dockerfile
# Shared Base Image (ssdlc-runner:latest) for Multi-Project Isolated SSDLC Runs
# All project containers share these read-only layers via Docker OverlayFS.
# Live CLI updates are served via /opt/ssdlc/.tools/bin or fast layer rebuild.
# ==============================================================================
FROM python:3.12-slim

COPY requirements.txt /opt/ssdlc-image-tools/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /opt/ssdlc-image-tools/requirements.txt

ENV DEBIAN_FRONTEND=noninteractive \
     PYTHONUNBUFFERED=1 \
    NPM_CONFIG_PREFIX="/opt/ssdlc-image-tools/npm-global" \
    UV_INSTALL_DIR="/opt/ssdlc-image-tools/bin" \
    PATH="/opt/ssdlc/.tools/bin:/opt/ssdlc/.tools/npm-global/bin:/opt/ssdlc-image-tools/bin:/opt/ssdlc-image-tools/npm-global/bin:/opt/host-bin:/usr/local/bin:/usr/bin:/bin"

# Layer 1: Cached OS dependencies + Node.js LTS (rarely changes)
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
      git curl jq tmux ca-certificates gnupg procps nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# Layer 2: Cached uv + gitleaks (rarely changes)
RUN mkdir -p /opt/ssdlc-image-tools/bin /opt/ssdlc-image-tools/npm-global && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    (apt-get update -qq && apt-get install -y -qq gitleaks || true) && \
    rm -rf /var/lib/apt/lists/*

# Layer 3: Fast-rebuildable AI CLI Layer (bust cache with --build-arg TOOLS_CACHEBUST=$(date +%s))
ARG TOOLS_CACHEBUST=1
RUN npm install -g @openai/codex@latest @anthropic-ai/claude-code@latest || true

WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/ssdlc/orchestrator.py"]
CMD ["doctor"]
