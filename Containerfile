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

# Playwright system dependencies (separate layer — large, rarely changes).
# The xorg-x11-server-* / libXfont2 / libXdmcp / xdotool entries are the X
# server that makes HEADED chromium possible for the chrome-agent skill
# (#410): this layer already carries the whole chromium GUI/graphics stack, so
# headed costs ~1.3 MB more rather than a new stack. They live here, above the
# harness cache-bust ARGs, so `lmer build --update-harness <name>` does not
# re-run a dnf install. Headless stays the default.
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
        xdg-utils \
        libXdmcp \
        libXfont2 \
        xdotool \
        xorg-x11-server-Xvfb \
        xorg-x11-server-common \
        xorg-x11-xauth && \
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

# Create working directory structure.
# The per-harness config dirs must pre-exist owned by developer: credential
# files are bind-mounted as individual files (mounts.py build_user_mounts),
# and docker/podman would otherwise create the missing parent directories
# root-owned — leaving the harness unable to write its own config/state next
# to the mounted file.
RUN mkdir -p /home/developer/.claude && \
    mkdir -p /home/developer/.codex && \
    mkdir -p /home/developer/.pi/agent && \
    mkdir -p /home/developer/.local/bin && \
    chown -R developer:developer /home/developer

# Chown /opt/tools to developer (directory created above for mise)
RUN chown -R developer:developer /opt/tools

# Create /Agents/global as the primary location for lmer tools
# This is where host directories will be mounted at runtime
# The .venv is built here and persists (not overwritten by mounts)
RUN mkdir -p /Agents/global /workspace /work /napkin /taskdef && \
    echo "CONTAINER_ENV=true" > /etc/container-environment && \
    echo "CONTAINER_TYPE=lmer" >> /etc/container-environment && \
    echo "RESOURCE_LIMITS=cpu:1,memory:2G,procs:512" >> /etc/container-environment && \
    chown -R developer:developer /Agents && \
    chown developer:developer /workspace /work /napkin /taskdef

# Codex lifecycle hooks installed by lmer are system-managed: they run without
# a per-session trust dialog while user/project hooks retain Codex's normal
# review gate. The requirements file names scripts from /Agents/global/hooks,
# which is part of this image (and live-mounted in self-development sessions).
RUN install -d -m 0755 /etc/codex
COPY --chown=root:root agent-files/codex/requirements.toml /etc/codex/requirements.toml

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

# Preinstall the superpowers plugin from the official marketplace so masterplan
# sessions resolve their dependency locally at start with no network round-trip.
# Masterplan itself is NOT baked here — it is installed at session start from the
# work-repo mirror (/work/mirrors/masterplan) when a session opts in via
# LMER_TASK=masterplan or a truthy LMER_MASTERPLAN (see libexec/claude-runner.sh).
#
# CRITICAL: `claude plugin marketplace add`/`install` persist their state
# (`extraKnownMarketplaces` + `enabledPlugins`) into ~/.claude/settings.json,
# creating a regular file there. Both runtime settings-link guards
# (Ctl/container/entrypoint.sh and libexec/claude-runner.sh) only link the global
# settings.json when it does NOT already exist (`[ ! -e ... ]`), so a baked
# settings.json would silently shadow the link for EVERY session — dropping the
# permissions allowlist, hooks, and statusLine. We therefore remove it as the
# last step of this RUN. The plugin files and install/marketplace records live
# under ~/.claude/plugins/ (installed_plugins.json, known_marketplaces.json) and
# survive the removal, so superpowers still resolves offline. With no
# `enabledPlugins` entry it defaults to disabled, so plain sessions pay no
# runtime (token) cost and an explicit `disable --all` is unnecessary; masterplan
# sessions re-enable it as a declared dependency of the masterplan plugin.
RUN claude plugin marketplace add https://github.com/anthropics/claude-plugins-official && \
    claude plugin install superpowers@claude-plugins-official && \
    rm -f /home/developer/.claude/settings.json

# === Alternative agent harnesses (all baked into the one image; selected at
# run time via LMER_HARNESS / lmer --harness — see docs/HARNESSES.md) ===

# Codex CLI (OpenAI). CODEX_CACHE_BUST busts this layer via
# `lmer build --update-harness codex`.
ARG CODEX_CACHE_BUST=0
RUN mise exec -- npm install -g @openai/codex

# pi (github.com/earendil-works/pi). --ignore-scripts per upstream install
# guidance. PI_CACHE_BUST busts this layer via `lmer build --update-harness pi`.
ARG PI_CACHE_BUST=0
RUN mise exec -- npm install -g --ignore-scripts @earendil-works/pi-coding-agent

# Install Playwright and Browsers
# The @playwright/mcp server package is gone with the MCP wiring it served
# (issue #410) — nothing referenced it any more. Playwright itself stays: it
# manages the chromium the chrome-agent skill drives, and it is still how
# browser tests are authored.
# We install globally to make the package available, and install browsers to user cache
# Use mise exec for both commands so node/npm are on PATH for reshimming and playwright
RUN mise exec -- bash -c "npm install -g playwright && playwright install chromium"

# === ROOT: browser-automation substrate for the chrome-agent skill ===
#
# captivus chrome-agent finds Chrome by testing five hardcoded absolute paths
# (/usr/bin/google-chrome, …-stable, /usr/bin/chromium-browser, /usr/bin/chromium,
# /snap/bin/chromium) — no PATH search, no env var, no flag. At run time we are uid
# 1000 and every one of those is unwritable (/usr/bin is root-owned; /snap cannot be
# created), so the shim has to be baked in here. os.path.isfile follows symlinks, so
# one symlink is enough.
#
# RETIREMENT TRIGGER: an upstream captivus PR adding a PATH search / CHROME_PATH /
# --chrome-path escape hatch to the launcher is in flight (carried by its own develop
# run, tracked separately from #410). Once that ships and the pin below moves to a
# release carrying it, pass CHROME_PATH instead and DELETE this symlink layer.
#
# The Playwright build directory is resolved, never hardcoded: a browser bump changes
# chromium-<build>, and a stale symlink would fail at launch time inside somebody's
# session rather than here. Zero or several matches fail the build loudly.
USER root
RUN set -eu; \
    matches="$(find /home/developer/.cache/ms-playwright \
                    -mindepth 3 -maxdepth 3 \
                    -path '*/chromium-*/chrome-linux64/chrome' -type f | sort)"; \
    count="$(printf '%s' "$matches" | grep -c '^' || true)"; \
    if [ "$count" -ne 1 ]; then \
        echo "FATAL: expected exactly 1 Playwright chromium, found $count:" >&2; \
        printf '%s\n' "$matches" >&2; \
        echo "Adjust the chrome-agent chromium shim in the Containerfile." >&2; \
        exit 1; \
    fi; \
    ln -sfn "$matches" /usr/bin/chromium; \
    test -x /usr/bin/chromium

USER developer

# captivus chrome-agent (PyPI `chrome-agent`) — the browser-automation substrate,
# driven through the chrome-agent skill rather than an MCP server. Pinned: 0.x,
# one maintainer, and the CDP verbs the skill teaches are what moves between minors.
# There is no cache-bust ARG: `lmer build` only emits them for registered
# harnesses (src/lmer_cli/build.py), so one here would be dead config. Moving
# the pin is what rebuilds this layer.
#
# NAME: captivus owns the `chrome-agent` verb here. sderosiaux/chrome-agent — a
# different project — installs an executable of the same name, so if a second CDP
# tool is ever added it gets a distinct name or a wrapper; the two must never
# coexist under one verb. See agent-files/claude/skills/chrome-agent/SKILL.md.
RUN uv tool install --python /usr/bin/python3.12 chrome-agent==0.5.8 && \
    chrome-agent --version


# Copy MCP configuration from repo. lmer ships no MCP server of its own (#410).
# This copy is the base the .mcp.local.json merge rewrites at container start —
# note it is NOT project scope for a session working in /workspace, which is
# where a project .mcp.json is read from. docs/MCP-CONFIGURATION.md has the
# whole scope story.
RUN mkdir -p /home/developer/.claude
COPY --chown=developer:developer .mcp.json /home/developer/.mcp.json

# === DEVELOPER: shell + git config (cached) ===

# Add container detection to profile
# CONTAINER_LIMITS is derived per shell from the container's own cgroup: the
# CPU, memory and pids limits are set per run (LMER_CPUS/LMER_MEMORY/
# LMER_PIDS_LIMIT), so a literal baked here would be wrong for every run that
# overrides one. The cgroup root is passed explicitly because a sourced script
# inherits the sourcing shell's positional parameters, and the script reads $1.
RUN echo 'export CLAUDE_CONTAINER=true' >> ~/.bashrc && \
    echo '[ -f /home/developer/container-limits.sh ] && . /home/developer/container-limits.sh /sys/fs/cgroup' >> ~/.bashrc && \
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
COPY --chown=developer:developer Ctl/container/container-limits.sh /home/developer/container-limits.sh
COPY --chown=developer:developer libexec/claude-runner.sh /home/developer/claude-runner.sh
RUN chmod +x /home/developer/entrypoint.sh /home/developer/container-limits.sh /home/developer/claude-runner.sh

# Verify FIPS mode
RUN python3 -c "import ssl; print('FIPS mode:', ssl.FIPS_mode())" || \
    echo "FIPS mode check not available in this Python version"

# === PROJECT SOURCE — LAST (changes frequently) ===

COPY --chown=developer:developer . .

# Install the project itself (editable) now that source is available
RUN source .venv/bin/activate && \
    /opt/tools/bin/uv sync

# PyYAML guarantee for the container entrypoint interpreter.
# The host CLI (src/lmer_cli/cli.py) launches the in-container clone/exec
# script with a bare `python3`. The ENV PATH above deliberately puts
# /Agents/global/.venv/bin ahead of /usr/bin, so that `python3` resolves to
# the uv-synced venv interpreter, which carries pyyaml>=6.0 from
# pyproject.toml / uv.lock. This RUN turns that PATH ordering from an
# accident into a build gate: the image build fails if `python3` ever stops
# resolving to the venv, or if the venv loses PyYAML.
RUN [ "$(command -v python3)" = "/Agents/global/.venv/bin/python3" ] && \
    python3 -c "import sys, yaml; print('PyYAML', yaml.__version__, 'via', sys.executable)"

# Copy agent-files/claude/ to .claude/ for Claude Code commands and settings
# (kept separate from .claude/ in repo to avoid conflicts with local user state)
RUN cp -r agent-files/claude/. .claude/

# Note: pre-commit hooks will be installed when running in an actual git repository

# Bake build provenance so any session can answer "what commit is this
# image?" — /Agents/global has no .git in-container, and on dev hosts the
# live-mounted subdirs can be NEWER than the image (BUILD_INFO describes
# the image; LMER_SOURCE_COMMIT in the session env describes the mounts).
# Placed after the heavy layers so a changing commit doesn't bust cache.
ARG LMER_BUILD_COMMIT=unknown
ENV LMER_BUILD_COMMIT=${LMER_BUILD_COMMIT}
RUN printf '%s\n' "${LMER_BUILD_COMMIT}" > /Agents/global/BUILD_INFO

# Set working directory to /workspace for convenience
WORKDIR /workspace

# Default command
ENTRYPOINT ["/home/developer/entrypoint.sh"]
CMD ["/bin/bash"]
