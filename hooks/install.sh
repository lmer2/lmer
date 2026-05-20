#!/bin/bash
# Installer script for Claude rule enforcement hooks

set -e

HOOKS_DIR="$(dirname "$0")"
GLOBAL_DIR="$(dirname "$HOOKS_DIR")"
BIN_DIR="$HOME/.local/bin"
CLAUDE_DIR="$HOME/.claude"

echo "🔧 Installing Claude Rule Enforcement Hooks..."
echo "============================================"

# Create necessary directories
echo "📁 Creating directories..."
mkdir -p "$BIN_DIR"
mkdir -p "$CLAUDE_DIR"

# Symlink Claude Code slash commands and settings into ~/.claude/
echo "🔗 Linking Claude Code slash commands..."
if [ ! -e "$CLAUDE_DIR/commands" ]; then
    ln -sf "$GLOBAL_DIR/agent-files/claude/commands" "$CLAUDE_DIR/commands"
    echo "   ✅ ~/.claude/commands → agent-files/claude/commands"
else
    echo "   ℹ️  ~/.claude/commands already exists, skipping"
fi
if [ ! -e "$CLAUDE_DIR/settings.json" ]; then
    ln -sf "$GLOBAL_DIR/agent-files/claude/settings.json" "$CLAUDE_DIR/settings.json"
    echo "   ✅ ~/.claude/settings.json → agent-files/claude/settings.json"
else
    echo "   ℹ️  ~/.claude/settings.json already exists, skipping"
fi

# Make hook scripts executable
echo "🔐 Setting permissions..."
chmod +x "$HOOKS_DIR"/*.py

# Create wrapper scripts in ~/.local/bin
echo "📝 Creating command wrappers..."

# rgr wrapper
cat > "$BIN_DIR/rgr" << 'EOF'
#!/bin/bash
# Read Global Rules with enforcement
python3 ~/Agents/global/hooks/rgr.py
echo "📖 Now reading global rules..."
# Add your actual rule reading command here
EOF
chmod +x "$BIN_DIR/rgr"

# rgrpc wrapper
cat > "$BIN_DIR/rgrpc" << 'EOF'
#!/bin/bash
# Read Global Rules Please Commit with gate enforcement
python3 ~/Agents/global/hooks/rgrpc.py
EOF
chmod +x "$BIN_DIR/rgrpc"

# pc wrapper
cat > "$BIN_DIR/pc" << 'EOF'
#!/bin/bash
# Please Commit with gate verification
python3 ~/Agents/global/hooks/pc.py
EOF
chmod +x "$BIN_DIR/pc"

# Focused command wrappers
for cmd in rgr-git rgr-test rgr-code rgr-security rgr-docs rgr-ci rgr-deps; do
    cat > "$BIN_DIR/$cmd" << EOF
#!/bin/bash
# Focused rule reading for ${cmd#rgr-}
python3 ~/Agents/global/hooks/rgr_focused.py $cmd
EOF
    chmod +x "$BIN_DIR/$cmd"
done

# Check if ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "⚠️  WARNING: $BIN_DIR is not in your PATH"
    echo "Add this line to your ~/.bashrc or ~/.zshrc:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# Create git hooks
echo ""
echo "🔗 Installing git hooks..."

# Pre-commit hook
cat > "$GLOBAL_DIR/.git/hooks/pre-commit" << 'EOF'
#!/bin/bash
# Enforce pre-commit checks
echo "🔍 Running pre-commit checks..."

# Check if commit gate was passed
if [ ! -f "$HOME/.claude/gate_passages.log" ]; then
    echo "❌ ERROR: No COMMIT GATE passage found"
    echo "💡 Run 'rgrpc' to pass through the commit gate"
    exit 1
fi

# Check last passage time
last_passage=$(tail -1 "$HOME/.claude/gate_passages.log" 2>/dev/null | cut -d' ' -f1-2)
if [ -n "$last_passage" ]; then
    last_epoch=$(date -d "$last_passage" +%s 2>/dev/null || date -j -f "%Y-%m-%d %H:%M:%S" "$last_passage" +%s 2>/dev/null)
    current_epoch=$(date +%s)
    diff=$((current_epoch - last_epoch))

    if [ $diff -gt 3600 ]; then
        echo "⚠️  COMMIT GATE passage expired (>1 hour ago)"
        echo "💡 Run 'rgrpc' again"
        exit 1
    fi
fi

# Run actual pre-commit if installed
if command -v pre-commit &> /dev/null; then
    pre-commit run
fi
EOF
chmod +x "$GLOBAL_DIR/.git/hooks/pre-commit"

# Pre-push hook
cat > "$GLOBAL_DIR/.git/hooks/pre-push" << 'EOF'
#!/bin/bash
# Enforce push policy

# Get repository info
remote_url=$(git remote get-url origin 2>/dev/null)

# Check against allow list from LMER_PUSH_ALLOW_LIST env var
allowed=false
if [ -n "$LMER_PUSH_ALLOW_LIST" ]; then
    IFS=',' read -ra REPOS <<< "$LMER_PUSH_ALLOW_LIST"
    for repo in "${REPOS[@]}"; do
        repo=$(echo "$repo" | xargs)  # trim whitespace
        if [[ "$remote_url" == *"$repo"* ]]; then
            allowed=true
            break
        fi
    done
fi

if [ "$allowed" = false ]; then
    echo "🛑 PUSH BLOCKED: Repository not in allow list"
    echo "📍 Repository: $remote_url"
    echo ""
    echo "To push, you need explicit permission."
    echo "Set LMER_PUSH_ALLOW_LIST env var with comma-separated repo patterns."
    exit 1
fi

echo "✅ Repository is on allow list, push allowed"
EOF
chmod +x "$GLOBAL_DIR/.git/hooks/pre-push"

echo ""
echo "✅ Installation complete!"
echo ""
echo "📋 Available commands:"
echo "   rgr         - Read global rules with tracking"
echo "   rgrpc       - Read rules and run commit gate"
echo "   pc          - Commit with gate verification"
echo "   rgr-git     - Read git-specific rules"
echo "   rgr-test    - Read testing rules"
echo "   rgr-code    - Read code quality rules"
echo "   rgr-security- Read security rules"
echo "   rgr-docs    - Read documentation rules"
echo "   rgr-ci      - Read CI/CD rules"
echo "   rgr-deps    - Read dependency rules"
echo ""
echo "🚀 Next steps:"
echo "   1. Source your shell config or restart terminal"
echo "   2. Test with 'rgr' command"
echo "   3. Git hooks are now active in this repository"
