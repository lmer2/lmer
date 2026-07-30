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
#!/bin/sh
# Enforce push policy. git calls this as `pre-push <remote> <url>` and
# feeds "<local_ref> <local_sha> <remote_ref> <remote_sha>" lines on stdin.
#
# LMER_PUSH_ALLOW_LIST extended grammar — DUPLICATED implementation.
# Decision: the generated hook must run standalone in any checkout (plain
# POSIX sh, no python/lmer_cli available), so it mirrors the allow-list
# check from lmer_cli.gates.GateSystem.run_push_gate rather than
# delegating to `gate-push`. Grammar: comma-separated entries, each
# either `repo` (substring of the remote URL, branch refs ONLY) or
# `repo|refpattern` (glob against the fully-qualified ref, e.g.
# refs/tags/*). Malformed entries — empty half, more than one `|` — are
# IGNORED (never fail open). The repo half matches as a substring for a
# CONFIGURED remote and ANCHORED (host/path or the bare host — both pin
# the host) when the push named a URL instead; a path-only entry
# authorizes nothing on that branch, and ref deletions are refused
# outright. Keep in sync
# with gates.py; the mirror is guarded by
# tests/test_push_allow_grammar_parity.py (#116).

# git passes the remote URL as $2 (for push-by-URL, $1 IS the URL and no
# configured remote exists) — prefer it, fall back to resolving $1, and
# FAIL CLOSED when neither yields a URL: exiting 0 here would skip the
# allow-list evaluation entirely, exactly like the run_push_gate
# regression this mirrors (push-by-URL must face the same list).
remote_name="${1:-origin}"
remote_url="${2:-}"
if [ -z "$remote_url" ]; then
    # `--push`: `git push` sends refs to remote.<name>.pushurl whenever it
    # is set, so the fetch url would authorize one repository while the
    # push lands on another (the gates.py/pc.py shape). Falls back to the
    # fetch url on its own when no pushurl is configured.
    remote_url=$(git remote get-url --push "$remote_name" 2>/dev/null)
fi
if [ -z "$remote_url" ]; then
    echo "🛑 PUSH BLOCKED: cannot determine the remote URL (fail closed)"
    exit 1
fi

# `git push <url> ...` names no configured remote: $1 IS the URL, so the
# string being matched came from the command line rather than from the
# operator's remote config. That branch matches ANCHORED (see
# url_entry_authorizes / GateSystem._url_entry_authorizes) — an unanchored
# substring would let an allowed path authorize any host embedding it.
#
# ASK GIT, don't guess from the string's shape: run_push_gate decides the
# same question by trying to resolve the remote and falling through to the
# URL branch when it cannot. Deciding by shape instead diverges on a name
# git does not have — `git push github.com main` would take the CONFIGURED
# branch (substring, no host pinned) here while run_push_gate fails closed.
if git remote get-url --push "$remote_name" >/dev/null 2>&1; then
    push_by_url=0
else
    push_by_url=1
fi

trim() {
    printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

# normalize_url URL: prints `host/path` (scheme, userinfo, port and a
# trailing `.git` stripped, lowercased) for the three URL forms git
# accepts, or nothing when the string names no repository (local path,
# bare host) — an empty result denies.
#
# The parse is ANCHORED, mirroring GateSystem._normalize_remote_url.
# Userinfo is stripped only where it can legally appear: inside the
# authority (before the first `/`) of a `scheme://` URL, and before the
# host of the scp-like form. Stripping at the last `@` of the WHOLE string
# instead would let an attacker-chosen host carrying the allowed identity
# in its PATH normalize to that identity —
# `https://evil.example.com/x@github.com/group/project` and
# `git@evil.invalid:x@github.com/group/project.git` must normalize to the
# evil host, not to `github.com/group/project`.
normalize_url() {
    _rest=$(trim "$1")
    _host=""
    _path=""
    case $_rest in
        *://*)
            # An empty or non-scheme prefix is not a URL: `urlsplit` only
            # reads an authority after a VALID scheme, so `://host/path`
            # (which git rejects outright — "protocol '' is not supported")
            # parses as a path there and names no repository. Deny rather
            # than treat the text after `://` as a host.
            _scheme=${_rest%%://*}
            case $_scheme in
                ''|*[!A-Za-z0-9+.-]*) return 0 ;;
                [!A-Za-z]*) return 0 ;;
            esac
            _rest=${_rest#*://}
            # A `#` or `?` ends the URL as far as the repository identity
            # goes (urlsplit strips fragment and query before the path).
            _rest=${_rest%%#*}
            _rest=${_rest%%\?*}
            case $_rest in
                */*) _authority=${_rest%%/*}; _path=${_rest#*/} ;;
                *)   _authority=$_rest;       _path="" ;;
            esac
            # Userinfo, then port — both live in the authority ONLY.
            case $_authority in *@*) _authority=${_authority##*@} ;; esac
            case $_authority in *:*) _authority=${_authority%%:*} ;; esac
            _host=$_authority ;;
        *)
            # scp-like `[user@]host:path`: userinfo may contain neither `@`
            # nor `/`, and the host neither `:` nor `/`, so the `@` and `:`
            # matched here are the real delimiters rather than characters
            # sitting inside the path.
            _after=$_rest
            case $_rest in
                *@*)
                    _maybe_user=${_rest%%@*}
                    case $_maybe_user in
                        ''|*/*) ;;                 # not a userinfo field
                        *) _after=${_rest#*@} ;;
                    esac ;;
            esac
            case $_after in
                *:*)
                    _before_colon=${_after%%:*}
                    case $_before_colon in
                        ''|*/*) ;;  # the `:` sits inside the path
                        *) _host=$_before_colon; _path=${_after#*:} ;;
                    esac ;;
            esac
            if [ -z "$_host" ]; then
                # Bare `host/path` — parsed from the ORIGINAL string, not
                # from the userinfo-stripped `$_after`. Without a scheme
                # and without a `:` before the first `/`, git reads the
                # whole thing as a local PATH and dials no host at all, so
                # there is no userinfo to strip: `user@github.com/group/project`
                # must deny (the `@` check below), not normalize to
                # `github.com/group/project`.
                case $_rest in
                    */*) _host=${_rest%%/*}; _path=${_rest#*/} ;;
                    *)   _host=$_rest;       _path="" ;;
                esac
                case $_host in
                    ''|*[@:]*) return 0 ;;
                esac
            fi ;;
    esac
    _path=${_path#/}
    _path=${_path%/}
    case $_path in *.git) _path=${_path%.git} ;; esac
    [ -n "$_host" ] && [ -n "$_path" ] || return 0
    printf '%s/%s' "$_host" "$_path" | tr '[:upper:]' '[:lower:]'
}

# url_entry_authorizes ENTRY URL: succeeds when ENTRY names one of the
# URL's two anchored identities — the full `host/path` or the bare `host`.
# A partial path component authorizes nothing, and neither does a
# path-only entry: any forge can serve the same path, so matching a bare
# path against an agent-supplied URL is the substring hole this closes
# spelled with a different prefix. Path-only grants remain valid for
# CONFIGURED remotes, whose URL came from the operator's git config.
url_entry_authorizes() {
    _normalized=$(normalize_url "$2")
    [ -n "$_normalized" ] || return 1
    _candidate=$(trim "$1")
    case $_candidate in
        *://*|*@*) _candidate=$(normalize_url "$_candidate") ;;
        *)
            _candidate=${_candidate#/}
            _candidate=${_candidate%/}
            case $_candidate in *.git) _candidate=${_candidate%.git} ;; esac
            _candidate=$(printf '%s' "$_candidate" | tr '[:upper:]' '[:lower:]') ;;
    esac
    [ -n "$_candidate" ] || return 1
    _url_host=${_normalized%%/*}
    [ "$_candidate" = "$_normalized" ] ||
        [ "$_candidate" = "$_url_host" ]
}

# ref_allowed REF: succeeds if some allow-list entry authorizes pushing
# fully-qualified REF to $remote_url.
ref_allowed() {
    _ref=$1
    _old_ifs=$IFS
    IFS=','
    set -f  # entries carry globs; keep them out of pathname expansion
    for _entry in $LMER_PUSH_ALLOW_LIST; do
        _entry=$(trim "$_entry")
        [ -n "$_entry" ] || continue
        case $_entry in
            *"|"*"|"*)  # >1 delimiter: malformed, ignore (fail closed)
                continue ;;
            *"|"*)
                _repo=$(trim "${_entry%%|*}")
                _pattern=$(trim "${_entry#*|}")
                # Empty half: malformed, ignore (fail closed)
                if [ -z "$_repo" ] || [ -z "$_pattern" ]; then
                    continue
                fi
                ;;
            *)
                _repo=$_entry
                _pattern="refs/heads/*"  # bare entry: branch refs only
                ;;
        esac
        if [ "$push_by_url" = "1" ]; then
            url_entry_authorizes "$_repo" "$remote_url" || continue
        else
            case $remote_url in
                *"$_repo"*) ;;
                *) continue ;;
            esac
        fi
        case $_ref in
            $_pattern)
                set +f
                IFS=$_old_ifs
                return 0 ;;
        esac
    done
    set +f
    IFS=$_old_ifs
    return 1
}

while read -r local_ref local_sha remote_ref remote_sha; do
    [ -n "$remote_ref" ] || continue
    # Deletion: git sends `(delete)` as the local ref and an all-zero local
    # sha. The allow list cannot express "may delete" — a deletion is at
    # least as destructive as a force push (release tags are immutable by
    # contract), so it is never approved here. Mirrors the empty-<src>
    # refusal in GateSystem._resolve_push_target_ref.
    is_delete=0
    if [ "$local_ref" = "(delete)" ]; then
        is_delete=1
    elif [ -n "$local_sha" ] && [ -z "$(printf '%s' "$local_sha" | tr -d '0')" ]; then
        is_delete=1
    fi
    if [ "$is_delete" = "1" ]; then
        echo "🛑 PUSH BLOCKED: ref deletion is never approved by this hook"
        echo "📍 Repository: $remote_url"
        echo "📍 Ref: $remote_ref"
        echo ""
        echo "Deleting a remote ref is a human decision — do it with plain git,"
        echo "deliberately. Release tags are immutable by contract."
        exit 1
    fi
    if ! ref_allowed "$remote_ref"; then
        echo "🛑 PUSH BLOCKED: ref not in allow list"
        echo "📍 Repository: $remote_url"
        echo "📍 Ref: $remote_ref"
        echo ""
        echo "To push, you need explicit permission."
        echo "Set LMER_PUSH_ALLOW_LIST (comma-separated: repo or repo|refpattern,"
        echo "e.g. 'host/repo|refs/tags/*'; bare entries allow branch refs only)."
        exit 1
    fi
done

echo "✅ All pushed refs are on the allow list, push allowed"
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
