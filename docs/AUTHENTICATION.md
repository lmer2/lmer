# Container Authentication and SSH Setup

## Overview

This document describes how authentication works in containerized environments, covering SSH access for Git operations and Claude API authentication. It explains the different methods available, their security implications, and best practices.

---

## Table of Contents

1. [Container-Home Directory](#container-home-directory)
2. [SSH Authentication](#ssh-authentication)
3. [GitLab Token Authentication](#gitlab-token-authentication)
4. [Claude API Authentication](#claude-api-authentication)
5. [Security Considerations](#security-considerations)
6. [Troubleshooting](#troubleshooting)

---

## Container-Home Directory

### What is Container-Home?

The `container-home` directory is a persistent storage location that preserves user configuration and data across container sessions. It's located at:
- **Global install**: `~/.lmer/container-home/`
- **Development repo**: `<repo_root>/container-home/`

### Directory Structure

When first created, `container-home` includes:
```
container-home/
├── .ssh/          # SSH configuration (config, known_hosts)
├── .config/       # Application configurations
├── .local/share/  # Application data
├── .bash_history  # Shell command history
└── .gitconfig     # Git configuration
```

### Initial Setup

On first run, LMER automatically:
1. Creates the directory structure
2. Copies SSH configuration files (`config`, `known_hosts`) from `~/.ssh/`
3. Copies Git configuration from `~/.gitconfig`
4. Creates shell history file
5. **Does NOT copy SSH private keys** (for security)

### SSH Directory Implications

The `.ssh` directory in `container-home` is mounted into the container at `/home/developer/.ssh` with read-write access. This means:

- **SSH config files persist** across container sessions
- **Known hosts** are preserved, avoiding repeated host key verification
- **Private keys are NOT automatically copied** - you must either:
  - Use SSH agent forwarding (recommended)
  - Manually copy specific keys (less secure)

### Mounting Behavior

```python
# From container_home.py
ssh_dir = container_home / ".ssh"
if ssh_dir.exists():
    # Mounted as: /home/developer/.ssh:rw
    args += ["-v", f"{ssh_dir}:/home/developer/.ssh:rw{se}"]
```

**Important**: The SSH directory is mounted read-write, allowing you to:
- Add SSH keys manually if needed
- Update SSH config files
- Add new known hosts entries

---

## SSH Authentication

### Method 1: SSH Agent Forwarding (Recommended)

SSH agent forwarding is the **preferred method** for accessing Git repositories in containers. It provides secure, temporary access without copying private keys.

#### How It Works

1. **Automatic Detection**: When you run `lmer`, it checks if `SSH_AUTH_SOCK` is set in your environment
2. **Socket Mounting**: If found, it mounts your SSH agent socket into the container at `/ssh-agent`
3. **Environment Variable**: Sets `SSH_AUTH_SOCK=/ssh-agent` inside the container
4. **Transparent Access**: Git and SSH commands automatically use your host keys

#### Setup Instructions

**1. Ensure SSH Agent is Running on Host**
```bash
# Check if agent is running
echo $SSH_AUTH_SOCK

# If empty, start the agent
eval $(ssh-agent)

# Add your keys
ssh-add ~/.ssh/id_rsa
ssh-add ~/.ssh/id_ed25519

# Verify keys are loaded
ssh-add -l
```

**2. Run LMER**
```bash
# SSH agent will be automatically forwarded
lmer --no-task --exec -- bash

# You'll see this message if agent is detected:
# ✅ SSH agent forwarding enabled
```

**3. Test Inside Container**
```bash
# Test SSH access to Git providers
ssh -T git@github.com
ssh -T git@gitlab.com

# Clone private repositories
git clone git@github.com:your-org/private-repo.git
```

#### Implementation Details

From `mounts.py`:
```python
ssh_sock = os.environ.get("SSH_AUTH_SOCK")
if ssh_sock:
    # Mount agent socket read-only
    args += ["-v", f"{ssh_sock}:/ssh-agent:ro", "-e", "SSH_AUTH_SOCK=/ssh-agent"]
    ssh_agent_enabled = True
```

**Key Points**:
- Socket is mounted **read-only** for security
- Access is **session-based** - only available while host agent is running
- **No keys stored** in container - private keys never leave the host
- **Automatic cleanup** - no keys remain after container exit

#### SSH Config for Agent Forwarding

If you have multiple SSH keys, configure which one to use:

```bash
# In container-home/.ssh/config
Host github.com
    IdentityAgent /ssh-agent
    IdentitiesOnly yes
    IdentityFile ~/.ssh/id_ed25519  # Optional: specify key
```

### Method 2: Manual Key Copy (Less Secure)

If you prefer not to use agent forwarding, you can copy SSH keys into `container-home`:

```bash
# Copy specific keys
cp ~/.ssh/id_rsa* container-home/.ssh/
chmod 600 container-home/.ssh/id_rsa

# Copy public key too
cp ~/.ssh/id_rsa.pub container-home/.ssh/

# Then run lmer normally
lmer --no-task --exec -- bash
```

**Security Implications**:
- ⚠️ **Private keys are stored on disk** in container-home
- ⚠️ **Keys persist** across container sessions
- ⚠️ **Less secure** than agent forwarding
- ✅ **Works offline** - doesn't require host SSH agent

**Best Practice**: Only copy keys if absolutely necessary, and consider using deploy keys for CI/CD scenarios.

---

## GitLab Token Authentication

LMER uses GitLab tokens to authenticate HTTPS git operations. This is an alternative to SSH authentication and is particularly useful in environments where SSH agent forwarding is not available.

### Token Environment Variables

GitLab tokens are resolved in the following priority order:

#### For Work Repository (highest to lowest priority)

1. `GITLAB_TOKEN_worklog` - Dedicated token for the persistent work repo
2. Host-specific tokens (see below)
3. `GITLAB_TOKEN` - Generic fallback

#### For All Other Repositories (highest to lowest priority)

1. `GITLAB_TOKEN_{suffix}` - Host-specific token
2. `GITLAB_TOKEN` - Generic fallback

### Host Suffix Resolution

The `{suffix}` is the sanitized hostname: lowercase with dots and hyphens replaced by underscores.

| GitLab Host | Suffix | Environment Variables |
|-------------|--------|----------------------|
| `git.example.com` | `git_example_com` | `GITLAB_TOKEN_git_example_com` |
| `gitlab.myorg.com` | `gitlab_myorg_com` | `GITLAB_TOKEN_gitlab_myorg_com` |

### Example .env Configuration

```bash
# Dedicated token for work repository (highest priority for work repo)
GITLAB_TOKEN_worklog=glpat-xxxxxxxxxxxxxxxxxxxx

# Host-specific tokens (using sanitized hostname as suffix)
GITLAB_TOKEN_git_example_com=glpat-yyyyyyyyyyyyyyyyyyyy      # For git.example.com
GITLAB_TOKEN_gitlab_myorg_com=glpat-zzzzzzzzzzzzzzzzzzzz       # For gitlab.myorg.com

# Generic fallback (lowest priority)
GITLAB_TOKEN=glpat-aaaaaaaaaaaaaaaaaaaaaa
```

### URL Conversion Behavior

When a token is available, LMER automatically converts SSH URLs to HTTPS:

```
# Original (SSH)
git@gitlab.example.com:agents/work.git

# Converted (HTTPS with token)
https://oauth2:glpat-xxx@gitlab.example.com/agents/work.git
```

### Fallback to SSH

If no token is found for a host:
- The original SSH URL is preserved
- SSH agent forwarding is used (if available)
- No automatic retry with SSH if HTTPS auth fails

### Prefer SSH Over Tokens

To force SSH authentication even when tokens are available, set:

```bash
REPO_AUTH_PREFER_SSH=1
```

Accepted values: `1`, `true`, `yes` (case insensitive)

When set, LMER will:
- Skip all token lookups for URL conversion
- Preserve original SSH URLs
- Rely on SSH agent forwarding for authentication

This is useful when:
- Your SSH setup is more reliable than token auth
- You want consistent authentication across all repos
- You're debugging token-related issues

### Token Precedence Summary

```
Work Repo Clone:
  1. GITLAB_TOKEN_worklog
  2. GITLAB_TOKEN_{host-suffix}
  3. GITLAB_TOKEN
  4. (No token) → Use SSH

Target Repo Clone:
  1. GITLAB_TOKEN_{host-suffix}
  2. GITLAB_TOKEN
  3. (No token) → Use SSH
```

### Troubleshooting

#### 404 Errors from GitLab

A 404 error often indicates an authentication issue rather than a missing resource:

```bash
# Check which tokens are set
env | grep -E 'GITLAB_TOKEN'

# Verify the token works (replace with your host and variable)
curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN_git_example_com" https://git.example.com/api/v4/user
```

#### Wrong Token Used

If the wrong token is being used, check:
1. The sanitized hostname suffix (dots/hyphens become underscores)
2. Whether a generic `GITLAB_TOKEN` is overriding host-specific tokens

#### Token Not Found

```bash
# LMER shows this message when no token is found:
# 🔑 No GitLab token found for git.example.com (work repo will use SSH)

# Ensure SSH agent is running as fallback
ssh-add -l
```

---

## Claude API Authentication

Claude can authenticate using two methods: a credentials file (mounted from the host — used by Claude.ai subscription users) or an API key (environment variable). Most users in our org use the subscription path, so the credentials file is typical and the API key is the alternative. The two methods are documented in numerical order below for historical reasons; see the [Recommended Setup](#recommended-setup) at the bottom for which to choose.

### Method 1: API Key via Environment Variable

The simplest method uses the `CLAUDE_API_KEY` environment variable.

#### Setup

**On Host**:
```bash
# Set in your shell
export CLAUDE_API_KEY=your-api-key-here

# Or add to ~/.bashrc or ~/.zshrc
echo 'export CLAUDE_API_KEY=your-api-key-here' >> ~/.bashrc
```

**In Docker Compose**:
```yaml
environment:
  - CLAUDE_API_KEY=${CLAUDE_API_KEY}  # Passed from host environment
```

**In LMER**:
```bash
# API key is automatically passed from host environment
lmer --no-task --exec -- bash

# Verify inside container
echo $CLAUDE_API_KEY
```

#### Verification

```bash
# Inside container
echo $CLAUDE_API_KEY  # Should show your key

# Test Claude CLI
claude --version
claude chat "Hello"  # Should authenticate successfully
```

### Method 2: Credentials File (Mounted from Host)

Claude can also authenticate using credential files mounted from your host home directory.

#### Credential File Locations

Claude looks for credentials in these locations (in order):
1. `~/.claude/.credentials.json` (primary)
2. `~/.claude.json` (alternative)

#### LMER Implementation

LMER mounts credential files **selectively** to avoid ownership issues:

```python
# From mounts.py - build_user_mounts()
credentials_file = home / ".claude" / ".credentials.json"
if credentials_file.exists():
    # Mount only the credentials file, not entire .claude directory
    args += ["-v", f"{credentials_file}:/home/developer/.claude/.credentials.json:ro{se}"]

# Also mount .claude.json if it exists
if (home / ".claude.json").exists():
    args += ["-v", f"{home}/.claude.json:/home/developer/.claude.json:rw{se}"]
```

**Key Points**:
- Only the **credentials file** is mounted (not entire `.claude` directory)
- Mounted as **read-only** for security
- Avoids ownership/permission issues with other `.claude` subdirectories

#### Docker Compose Implementation

Docker Compose mounts the **entire `.claude` directory**:

```yaml
volumes:
  # Mount Claude configuration for authentication
  - ${HOME}/.claude:/home/developer/.claude:rw
  - ${HOME}/.config/claude:/home/developer/.config/claude:rw
```

**Difference**: Docker Compose mounts the full directory (read-write), while LMER mounts only the credentials file (read-only).

#### Verification

The `claude-runner.sh` script checks for credentials:

```bash
# Check if credentials file exists
if [ -f "$HOME/.claude/.credentials.json" ]; then
    echo "✅ Credentials found in .claude/"
else
    echo "❌ No credentials found at $HOME/.claude/.credentials.json"
fi

# Check for .claude.json
if [ -f "$HOME/.claude.json" ]; then
    echo "✅ Claude config found at ~/.claude.json"
fi
```

### Credential File Format

The `.credentials.json` file typically contains:

```json
{
  "apiKey": "your-api-key-here",
  "organizationId": "optional-org-id"
}
```

Or in `.claude.json`:
```json
{
  "claude_api_key": "your-api-key-here"
}
```

### Priority Order

Claude checks authentication in this order:
1. **Environment variable** `CLAUDE_API_KEY` (highest priority)
2. **Credentials file** `~/.claude/.credentials.json`
3. **Config file** `~/.claude.json`

---

## Security Considerations

### SSH Security

#### Agent Forwarding (Recommended)
- ✅ **No key storage** - private keys never copied to container
- ✅ **Read-only mount** - agent socket mounted read-only
- ✅ **Session-based** - access only while host agent is running
- ✅ **Automatic cleanup** - no keys remain after container exit
- ✅ **Forwarding isolation** - container can't access keys directly

#### Manual Key Copy
- ⚠️ **Keys stored on disk** - private keys persist in container-home
- ⚠️ **File permissions** - must ensure proper permissions (600)
- ⚠️ **Backup risk** - keys included if container-home is backed up
- ⚠️ **Key rotation** - requires manual updates

### Claude API Security

#### API Key (Environment Variable)
- ✅ **Not persisted** - key only in memory during session
- ✅ **Easy rotation** - update environment variable
- ⚠️ **Process visibility** - visible in process list
- ⚠️ **Logging risk** - may appear in logs if not careful

#### Credentials File
- ✅ **Read-only mount** - credentials file mounted read-only in LMER
- ⚠️ **File persistence** - credentials stored on host disk
- ⚠️ **Backup risk** - credentials included if `.claude` is backed up
- ⚠️ **Ownership issues** - Docker Compose mounts entire directory (rw)

### Best Practices

1. **Use SSH Agent Forwarding** for Git operations
2. **Prefer API Key** over credentials file when possible
3. **Rotate credentials regularly**
4. **Never commit credentials** to repositories
5. **Use deploy keys** for CI/CD scenarios
6. **Monitor credential access** in container logs
7. **Use read-only mounts** when possible

---

## Troubleshooting

### SSH Issues

#### Agent Not Detected

**Symptoms**: No "SSH agent forwarding enabled" message

**Solution**:
```bash
# On host, check agent
ssh-add -l

# Should show your keys, if not:
ssh-add ~/.ssh/id_rsa
```

#### Permission Denied

**Symptoms**: SSH operations fail with permission denied

**Solution**:
```bash
# Inside container, check agent is accessible
echo $SSH_AUTH_SOCK  # Should show: /ssh-agent
ls -la $SSH_AUTH_SOCK  # Should show socket file

# Check SSH config
cat ~/.ssh/config

# Verify key is loaded on host
ssh-add -l
```

#### Multiple Keys Not Working

**Symptoms**: Wrong key used for Git operations

**Solution**:
```bash
# Configure SSH config in container-home/.ssh/config
Host github.com
    IdentityAgent /ssh-agent
    IdentitiesOnly yes
    IdentityFile ~/.ssh/id_ed25519
```

### Claude Authentication Issues

#### API Key Not Working

**Symptoms**: Claude CLI fails to authenticate

**Solution**:
```bash
# Verify key is set
echo $CLAUDE_API_KEY  # Should show your key

# Check in container
lmer --no-task --exec -- bash
echo $CLAUDE_API_KEY  # Should show your key

# Test Claude CLI
claude --version
```

#### Credentials File Not Found

**Symptoms**: "No credentials found" message

**Solution**:
```bash
# Check credentials file exists on host
ls -la ~/.claude/.credentials.json
ls -la ~/.claude.json

# Verify mount inside container
lmer --no-task --exec -- bash
ls -la ~/.claude/.credentials.json
cat ~/.claude/.credentials.json  # Should show credentials
```

#### Ownership/Permission Issues

**Symptoms**: Cannot read credentials file

**Solution**:
```bash
# Check file permissions on host
ls -la ~/.claude/.credentials.json

# Fix permissions if needed
chmod 600 ~/.claude/.credentials.json

# For Docker Compose, ensure user ID matches
# Check container user
docker exec claude-cli-safe id

# Check host user
id
```

### Container-Home Issues

#### SSH Directory Not Created

**Symptoms**: `.ssh` directory missing in container-home

**Solution**:
```bash
# Manually create directory structure
mkdir -p container-home/.ssh
chmod 700 container-home/.ssh

# Copy SSH config files
cp ~/.ssh/config container-home/.ssh/
cp ~/.ssh/known_hosts container-home/.ssh/
chmod 600 container-home/.ssh/*
```

#### Keys Not Persisting

**Symptoms**: SSH keys disappear between container sessions

**Solution**:
```bash
# Verify container-home is mounted correctly
lmer --no-task --exec -- bash
ls -la ~/.ssh  # Should show your keys

# Check mount in container
mount | grep ssh

# Verify container-home path
# Should be: ~/.lmer/container-home/ (global) or <repo>/container-home/ (dev)
```

---

## Summary

### Quick Reference

| Method | SSH | Claude API | Security | Persistence |
|--------|-----|-----------|----------|-------------|
| **Agent Forwarding** | ✅ Recommended | N/A | ✅ High | ❌ Session-only |
| **Manual Key Copy** | ⚠️ Less secure | N/A | ⚠️ Medium | ✅ Persistent |
| **Credentials File** | N/A | ✅ Recommended (subscription users) | ⚠️ Medium | ✅ Persistent |
| **API Key (env var)** | N/A | ✅ Alternative | ✅ High | ❌ Session-only |

### Recommended Setup

**For SSH**:
1. Use SSH agent forwarding (automatic with LMER)
2. Configure `container-home/.ssh/config` for multiple keys
3. Only copy keys if absolutely necessary

**For Claude**:
1. If you have a Claude.ai subscription: run `/login` once on the host inside `claude` to populate `~/.claude/.credentials.json`; LMER mounts it automatically — no API key needed.
2. Otherwise, set `CLAUDE_API_KEY` (e.g. in `~/.lmer/.env` or your shell environment).
3. Ensure proper file permissions on credential files.

**For Both**:
1. Never commit credentials to repositories
2. Rotate credentials regularly
3. Use read-only mounts when possible
4. Monitor access in container logs

---

**Remember**: Security is paramount. Always prefer methods that don't persist sensitive data in containers, and use agent forwarding or environment variables when possible.
