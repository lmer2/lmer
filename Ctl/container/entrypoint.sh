#!/bin/bash
# Entrypoint script for Oracle Linux 9 FIPS container

set -e

# First, check container environment and safety
echo "🔍 Checking container environment..."
if [ -f /Agents/global/Ctl/container/check-environment.py ]; then
    python3 /Agents/global/Ctl/container/check-environment.py
    echo ""
else
    echo "⚠️  WARNING: Environment checker not found!"
    echo "⚠️  Running without safety verification!"
    echo ""
fi

# Staged bind mounts → the paths their harnesses expect (#293/#290); must run
# before anything reads the home directory. Absolute path so dev-mode sessions
# pick up linker fixes without an image rebuild. `|| true` because under
# `set -e` a linker failure would cost the whole session.
if [ -f /Agents/global/Ctl/container/setup-mount-links.sh ]; then
    bash /Agents/global/Ctl/container/setup-mount-links.sh || true
fi

# Verify FIPS mode is enabled
echo "🔒 Checking FIPS mode status..."
if [ -f /proc/sys/crypto/fips_enabled ]; then
    fips_status=$(cat /proc/sys/crypto/fips_enabled)
    if [ "$fips_status" = "1" ]; then
        echo "✅ FIPS mode is enabled"
    else
        echo "❌ WARNING: FIPS mode is not enabled (status: $fips_status)"
        echo "   Note: Container runs with host kernel FIPS setting"
    fi
else
    echo "⚠️  Cannot verify FIPS status (file not found)"
fi

# Verify OpenSSL FIPS
echo ""
echo "🔐 Checking OpenSSL FIPS configuration..."
openssl version -a | grep -i fips || echo "⚠️  OpenSSL FIPS information not found"

# Add Claude, uv, and global bin to PATH
export PATH="/Agents/global/bin:/home/developer/.npm-global/bin:/home/developer/.local/bin:/home/developer/.local/share/mise/shims:${PATH}"

# Ensure mise tools are available (bind mounts override build-time installs)
if [ -f /Agents/global/.mise.toml ] && command -v mise >/dev/null 2>&1; then
    mkdir -p /home/developer/.config/mise
    if [ ! -f /home/developer/.config/mise/config.toml ]; then
        cp /Agents/global/.mise.toml /home/developer/.config/mise/config.toml
        mise trust /home/developer/.config/mise/config.toml >/dev/null 2>&1
    fi
    if ! command -v claude-powerline >/dev/null 2>&1; then
        echo "🔧 Installing mise tools..."
        mise install --yes >/dev/null 2>&1
        mise reshim >/dev/null 2>&1
        echo "✅ Mise tools installed"
    fi
fi

# Set up Python environment
echo ""
echo "🐍 Activating container Python virtual environment..."

# The container has its own venv at /Agents/global/.venv built during image creation
# This venv is NOT mounted from the host - it uses the container's Python version
if [ -f /Agents/global/.venv/bin/activate ]; then
    source /Agents/global/.venv/bin/activate
    echo "✅ Activated /Agents/global/.venv"
else
    echo "⚠️  Container venv not found at /Agents/global/.venv"
fi

# Also add mounted src to PYTHONPATH for latest code from host
export PYTHONPATH="/Agents/global/src:${PYTHONPATH}"

# Pin the lmer-side Python interpreter so infra scripts (hooks/, claude-runner.sh)
# are not affected by the workspace-venv activation that may follow. A project's
# /workspace/.venv typically lacks lmer's deps like jinja2 and would otherwise
# shadow `python3` via PATH for every infra invocation.
export LMER_PYTHON="/Agents/global/.venv/bin/python3"

# Check for workspace venv (project-specific)
if [ -f /workspace/.venv/bin/activate ]; then
    echo "✅ Found workspace virtual environment"
    source /workspace/.venv/bin/activate
elif [ -f /workspace/venv/bin/activate ]; then
    echo "✅ Found workspace venv directory"
    source /workspace/venv/bin/activate
fi

# Verify Python can use FIPS-compliant crypto
echo ""
echo "🔍 Testing Python cryptography..."
"${LMER_PYTHON:-python3}" -c "
import hashlib
import ssl
try:
    # Test FIPS-approved algorithm
    h = hashlib.sha256()
    h.update(b'test')
    print('✅ SHA-256 (FIPS-approved): OK')

    # This should fail in strict FIPS mode
    try:
        h = hashlib.md5()
        h.update(b'test')
        print('⚠️  MD5 (non-FIPS): Available (FIPS may not be enforced)')
    except:
        print('✅ MD5 (non-FIPS): Blocked (FIPS enforced)')

except Exception as e:
    print(f'❌ Crypto test failed: {e}')
"

# ── Git identity overrides ──
# When LMER_GIT_USER_NAME / LMER_GIT_USER_EMAIL are set, export git's native
# author/committer env vars so commits made this session use the override.
# This deliberately does NOT run `git config --global`: the container's
# ~/.gitconfig is a persistent, read-write bind mount (container-home/.gitconfig),
# so writing to it would leak the override into future sessions even after the
# env var is unset. Git's GIT_AUTHOR_*/GIT_COMMITTER_* vars are session-scoped,
# touch no file, and apply across every repo (workspace and the work repo).
# Either var may be set independently; the unset half falls back to gitconfig.
if [ -n "$LMER_GIT_USER_NAME" ]; then
    export GIT_AUTHOR_NAME="$LMER_GIT_USER_NAME"
    export GIT_COMMITTER_NAME="$LMER_GIT_USER_NAME"
    echo "✅ Git author/committer name overridden via LMER_GIT_USER_NAME"
fi
if [ -n "$LMER_GIT_USER_EMAIL" ]; then
    export GIT_AUTHOR_EMAIL="$LMER_GIT_USER_EMAIL"
    export GIT_COMMITTER_EMAIL="$LMER_GIT_USER_EMAIL"
    echo "✅ Git author/committer email overridden via LMER_GIT_USER_EMAIL"
fi

# ── GPG agent forwarding ──
# Provision a GNUPGHOME pointing gpg at the forwarded host agent socket
# (#355). Modern gpg takes the agent socket from its home directory and
# ignores the environment for it, so this redirect file IS the mechanism.
#
# Where the redirect has to live is gnupg's choice, not ours: gpg opens
# S.gpg-agent inside its *socket directory*, which is a subdirectory of
# /run/user/<uid>/gnupg when that runtime dir exists and the home directory
# only as a fallback when it does not (gpgconf says so itself: "no /run/user
# dir / using homedir as fallback"). So ask gpgconf where it will look
# rather than assuming — a container that ever gains /run/user/<uid> would
# otherwise get a redirect gpg never reads, and would report success while
# every decrypt failed. The separate home is for isolation from any real
# ~/.gnupg in the session; it has no bearing on the socket directory.
#
# Inert unless the socket mount is actually present, so every other taskdef
# sees nothing and pays nothing — and non-fatal throughout: a broken gpg
# setup may cost the session gpg, never the launch.
if [ -n "$LMER_ANSIBLE_GPG_AGENT" ] && [ -S "$LMER_ANSIBLE_GPG_AGENT" ]; then
    lmer_gnupg_home="${HOME:-/home/developer}/.lmer-gnupg"
    lmer_gnupg_sockdir=""
    if mkdir -p "$lmer_gnupg_home" && chmod 700 "$lmer_gnupg_home"; then
        lmer_gnupg_sockdir="$(GNUPGHOME="$lmer_gnupg_home" \
            gpgconf --list-dirs socketdir 2>/dev/null)" || lmer_gnupg_sockdir=""
        # No gpgconf, or nothing printed: fall back to the home directory,
        # which is where gpg looks whenever there is no /run/user dir.
        [ -n "$lmer_gnupg_sockdir" ] || lmer_gnupg_sockdir="$lmer_gnupg_home"
    fi
    if [ -n "$lmer_gnupg_sockdir" ] \
        && mkdir -p "$lmer_gnupg_sockdir" \
        && chmod 700 "$lmer_gnupg_sockdir" \
        && printf '%%Assuan%%\nsocket=%s\n' "$LMER_ANSIBLE_GPG_AGENT" \
            > "$lmer_gnupg_sockdir/S.gpg-agent"; then
        export GNUPGHOME="$lmer_gnupg_home"
        echo "✅ gpg-agent forwarded via $LMER_ANSIBLE_GPG_AGENT (GNUPGHOME=$GNUPGHOME, socketdir=$lmer_gnupg_sockdir)"
        # The keyring is copied rather than read in place: gpg wants its
        # lockfiles and trustdb beside the keyring, and the mount is ro. The
        # destination name carries the format — gpg picks its parser from it
        # — so it mirrors the mounted suffix.
        if [ -n "$LMER_ANSIBLE_GPG_PUBRING" ] && [ -f "$LMER_ANSIBLE_GPG_PUBRING" ]; then
            case "$LMER_ANSIBLE_GPG_PUBRING" in
                *.kbx) lmer_pubring_name="pubring.kbx" ;;
                *)     lmer_pubring_name="pubring.gpg" ;;
            esac
            if cp "$LMER_ANSIBLE_GPG_PUBRING" "$lmer_gnupg_home/$lmer_pubring_name" \
                && chmod 600 "$lmer_gnupg_home/$lmer_pubring_name"; then
                echo "✅ gpg public keyring installed as $lmer_pubring_name"
            else
                echo "⚠️  Could not install the gpg public keyring — recipient lookups will fail"
            fi
        else
            echo "⚠️  No gpg public keyring mounted — recipient lookups will fail"
        fi
    else
        echo "⚠️  Could not provision $lmer_gnupg_home — gpg-agent forwarding is off this session"
    fi
fi

# Check if we're in a git repository
if [ -d .git ]; then
    echo ""
    echo "📁 Git repository detected"

    # Install git hooks if not already installed and install script exists
    if [ ! -f .git/hooks/pre-commit ] && [ -f ./hooks/install.sh ]; then
        echo "🔧 Installing git hooks..."
        ./hooks/install.sh 2>/dev/null || echo "⚠️  Could not install git hooks"
    fi
fi

# Set up aliases for convenience
echo ""
echo "🚀 Setting up command aliases..."
alias ll='ls -la'
alias gs='git status'
alias gd='git diff'
alias pytest='python -m pytest'

# Show available commands
echo ""
echo "📋 Available enforcement commands:"
echo "   rgr         - Read global rules"
echo "   rgrpc       - Read rules and run commit gate"
echo "   pc          - Please commit with verification"
echo "   rgr-git     - Read git-specific rules"
echo "   rgr-test    - Read testing rules"
echo "   rgr-security- Read security rules"
echo ""
echo "🧪 Run tests with: pytest tests/"
echo "📖 View rules with: rgr"
echo ""

# Set up Claude Code slash commands and settings symlinks
# Source files live in agent-files/claude/ (separate from .claude/ to avoid bind mount conflicts)
CLAUDE_HOME="/home/developer/.claude"
if [ -d "/Agents/global/agent-files/claude/commands" ] && [ ! -e "$CLAUDE_HOME/commands" ]; then
    ln -sf /Agents/global/agent-files/claude/commands "$CLAUDE_HOME/commands"
    echo "✅ Global slash commands linked to Claude home"
fi
if [ ! -e "$CLAUDE_HOME/settings.json" ] && [ -f "/Agents/global/agent-files/claude/settings.json" ]; then
    if [ "$LMER_DANGER_ZONE" = "1" ]; then
        # Danger zone: keep statusLine from source, set permissions to bypass everything
        # (skipDangerousModePermissionPrompt + skipAutoPermissionPrompt + defaultMode=bypassPermissions
        # together prevent every kind of permission prompt — the user opted into danger zone)
        DANGER_PERMS='{
          "skipDangerousModePermissionPrompt": true,
          "skipAutoPermissionPrompt": true,
          "permissions": { "defaultMode": "bypassPermissions" }
        }'
        if command -v jq >/dev/null 2>&1; then
            # Shallow-merge over the FULL global settings so hooks (SessionEnd
            # run-state release, Stop slack guard) survive — the earlier shape
            # kept only statusLine and silently dropped every hook in
            # danger-zone sessions. $perms still replaces the permissions
            # object wholesale, which is fine: allow-lists are moot under
            # bypassPermissions.
            jq --argjson perms "$DANGER_PERMS" \
               '. + $perms' \
               /Agents/global/agent-files/claude/settings.json > "$CLAUDE_HOME/settings.json"
        else
            # Fallback without jq: no statusLine/hooks, but still bypass prompts
            echo "$DANGER_PERMS" > "$CLAUDE_HOME/settings.json"
        fi
        echo "⚡ Danger zone: settings.json written with bypass-permissions mode"
    else
        ln -sf /Agents/global/agent-files/claude/settings.json "$CLAUDE_HOME/settings.json"
        echo "✅ Global settings.json linked to Claude home"
    fi
fi

# Execute command passed to docker run
exec "$@"
