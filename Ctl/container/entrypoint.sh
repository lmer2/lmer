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
            jq --argjson perms "$DANGER_PERMS" \
               '{statusLine} + $perms' \
               /Agents/global/agent-files/claude/settings.json > "$CLAUDE_HOME/settings.json"
        else
            # Fallback without jq: no statusLine, but still bypass prompts
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
