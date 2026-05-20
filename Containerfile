FROM oraclelinux:9-slim-fips

LABEL org.opencontainers.image.title="lmer"
LABEL org.opencontainers.image.description="LLM Environment Runtime — FIPS-compliant dev container"

# FIPS mode is already enabled in the 9-slim-fips base image
# No need to install crypto-policies-scripts separately

# === ROOT: base image + all system dependencies ===

# Install base dependencies
# Using Python 3.12 to satisfy pyproject.toml requires-python >= 3.12
RUN microdnf -y install \
        python3.12 \
        python3.12-pip \
        python3.12-devel \
        git \
        gcc \
        gcc-c++ \
        make \
        openssl \
        openssl-devel \
        which \
        sudo \
        shadow-utils && \
    microdnf clean all && \
    # Set python3.12 as the default python3
    alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 && \
    alternatives --set python3 /usr/bin/python3.12

ARG RUNTIME_DEPS="jq curl wget vim tmux tree the_silver_searcher procps-ng"

# EPEL + development tools
RUN microdnf -y install epel-release && \
    microdnf -y install $RUNTIME_DEPS && \
    microdnf clean all

# Add Docker CE repo for docker-cli (needed for service mode target-exec)
RUN cat > /etc/yum.repos.d/docker-ce.repo <<'REPO'
[docker-ce-stable]
name=Docker CE Stable - $basearch
baseurl=https://download.docker.com/linux/centos/$releasever/$basearch/stable
enabled=1
gpgcheck=1
gpgkey=https://download.docker.com/linux/centos/gpg
REPO

RUN microdnf -y install docker-ce-cli && \
    microdnf clean all

# Install mise to /opt/tools (persists when home is bind-mounted)
RUN mkdir -p /opt/tools/bin && \
    curl -fsSL https://mise.run | MISE_INSTALL_PATH=/opt/tools/bin/mise sh

# Playwright system dependencies (separate layer — large, rarely changes)
RUN microdnf -y install \
        alsa-lib \
        at-spi2-atk \
        at-spi2-core \
        cups-libs \
        gtk3 \
        libXcomposite \
        libXdamage \
        libXext \
        libXfixes \
        libXrandr \
        libdrm \
        libxkbcommon \
        libxshmfence \
        mesa-libgbm \
        nss \
        pango \
        xdg-utils && \
    microdnf clean all

# === ROOT: user creation + directory setup ===

# Build arguments for user ID (optional, defaults to system-assigned UID)
ARG BUILD_UID
ARG BUILD_GID

# Create non-root user for development
# Only for development - NOPASSWD allows testing without password prompts
# If BUILD_UID/BUILD_GID are provided, create user with those IDs
RUN if [ -n "$BUILD_UID" ] && [ -n "$BUILD_GID" ]; then \
        groupadd -g "$BUILD_GID" developer && \
        useradd -m -s /bin/bash -u "$BUILD_UID" -g "$BUILD_GID" developer; \
    else \
        useradd -m -s /bin/bash developer; \
    fi && \
    echo "developer ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Set up Python environment with FIPS-compliant settings
ENV PYTHONHASHSEED=0
ENV OPENSSL_CONF=/etc/pki/tls/openssl.cnf
ENV OPENSSL_FIPS=1

# Create working directory structure
RUN mkdir -p /home/developer/.claude && \
    mkdir -p /home/developer/.local/bin && \
    chown -R developer:developer /home/developer

# Chown /opt/tools to developer (directory created above for mise)
RUN chown -R developer:developer /opt/tools

# Create /Agents/global as the primary location for lmer tools
# This is where host directories will be mounted at runtime
# The .venv is built here and persists (not overwritten by mounts)
RUN mkdir -p /Agents/global /workspace /work && \
    echo "CONTAINER_ENV=true" > /etc/container-environment && \
    echo "CONTAINER_TYPE=lmer" >> /etc/container-environment && \
    echo "RESOURCE_LIMITS=cpu:1,memory:2G,procs:512" >> /etc/container-environment && \
    chown -R developer:developer /Agents && \
    chown developer:developer /workspace /work

# === DEVELOPER: uv + python deps (cached until pyproject.toml/uv.lock change) ===

# Switch to non-root user for all user-space installations
USER developer
WORKDIR /Agents/global

# Install uv to /opt/tools (persists when home is bind-mounted)
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/opt/tools/bin sh && \
    sed -i '/\.local\/bin\/env/d' ~/.bashrc && \
    sed -i '/\.local\/bin\/env/d' ~/.bash_profile 2>/dev/null || true

# Copy ONLY dependency-defining files first (change rarely)
# .mise.toml included so mise trust works before full COPY
COPY --chown=developer:developer pyproject.toml uv.lock .mise.toml ./

# Set up Python virtual environment with uv at /Agents/global/.venv
# Use system Python 3.12 which satisfies pyproject.toml requires-python >= 3.12
# This venv persists because we mount individual subdirectories, not .venv
# --no-install-project: only install dependencies (cached until pyproject.toml or uv.lock change)
RUN /opt/tools/bin/uv venv --python /usr/bin/python3.12 && \
    source .venv/bin/activate && \
    /opt/tools/bin/uv sync --no-install-project

# === DEVELOPER: mise tools, npm, CLI tools (cached) ===

# Set PATH early so mise shims and npm can find mise during build steps
ENV PATH="/opt/tools/bin:/home/developer/.npm-global/bin:/home/developer/.local/bin:/Agents/global/.venv/bin:${PATH}"

# Setup mise for developer user (mise installed to /opt/tools above)
RUN echo 'eval "$(/opt/tools/bin/mise activate bash)"' >> ~/.bashrc && \
    mkdir -p ~/.config/mise

# Copy mise configuration to the trusted location
COPY --chown=developer:developer .mise.toml /home/developer/.config/mise/config.toml

# Trust mise configs and install tools defined in config (node, gh, glab, etc.)
RUN mise trust /home/developer/.config/mise/config.toml && \
    mise trust /Agents/global/.mise.toml && \
    mise install --yes

# Configure npm to use a user-local prefix so we can install global packages
# Uses mise exec to run npm from mise-managed node
RUN mkdir -p /home/developer/.npm-global && \
    /opt/tools/bin/mise exec -- npm config set prefix '/home/developer/.npm-global'

# Install Claude Code CLI using native installer
# CLAUDE_CACHE_BUST is used to force re-pull when --update-claude is passed to lmer build
ARG CLAUDE_CACHE_BUST=0
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install Playwright MCP and Browsers
# We install globally to make the package available, and install browsers to user cache
# Use mise exec for both commands so node/npm are on PATH for reshimming and playwright
RUN mise exec -- bash -c "npm install -g @playwright/mcp playwright && playwright install chromium"

# Configure Playwright MCP server for Claude Code
RUN mkdir -p /home/developer/.claude

# Copy MCP configuration from repo (.mcp.json contains MCP server definitions)
COPY --chown=developer:developer .mcp.json /home/developer/.mcp.json

# === DEVELOPER: shell + git config (cached) ===

# Add container detection to profile
RUN echo 'export CLAUDE_CONTAINER=true' >> ~/.bashrc && \
    echo 'export CONTAINER_LIMITS="CPU:1core Memory:2GB Processes:512"' >> ~/.bashrc && \
    echo '[ -d /Agents/global ] && export GLOBAL_RULES_MOUNTED=true || echo "⚠️ WARNING: /Agents/global not mounted - running without safety rules!"' >> ~/.bashrc && \
    echo '# Auto-activate workspace venv if present' >> ~/.bashrc && \
    echo 'if [ -f /workspace/.venv/bin/activate ]; then' >> ~/.bashrc && \
    echo '    source /workspace/.venv/bin/activate' >> ~/.bashrc && \
    echo 'elif [ -f /workspace/venv/bin/activate ]; then' >> ~/.bashrc && \
    echo '    source /workspace/venv/bin/activate' >> ~/.bashrc && \
    echo 'fi' >> ~/.bashrc

ENV CLAUDE_CONTAINER=true

# Set up git configuration
RUN git config --global user.name "Developer" && \
    git config --global user.email "developer@example.com" && \
    git config --global init.defaultBranch main

# === DEVELOPER: entrypoint (cached, rarely changes) ===

# Create entrypoint script
COPY --chown=developer:developer Ctl/container/entrypoint.sh /home/developer/entrypoint.sh
COPY --chown=developer:developer libexec/claude-runner.sh /home/developer/claude-runner.sh
RUN chmod +x /home/developer/entrypoint.sh /home/developer/claude-runner.sh

# Verify FIPS mode
RUN python3 -c "import ssl; print('FIPS mode:', ssl.FIPS_mode())" || \
    echo "FIPS mode check not available in this Python version"

# === PROJECT SOURCE — LAST (changes frequently) ===

COPY --chown=developer:developer . .

# Install the project itself (editable) now that source is available
RUN source .venv/bin/activate && \
    /opt/tools/bin/uv sync

# Copy agent-files/claude/ to .claude/ for Claude Code commands and settings
# (kept separate from .claude/ in repo to avoid conflicts with local user state)
RUN cp -r agent-files/claude/. .claude/

# Note: pre-commit hooks will be installed when running in an actual git repository

# Set working directory to /workspace for convenience
WORKDIR /workspace

# Default command
ENTRYPOINT ["/home/developer/entrypoint.sh"]
CMD ["/bin/bash"]
