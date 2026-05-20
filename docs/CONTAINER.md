# Container Runtime Support

This project provides a FIPS 140-2 compliant development environment using Oracle Linux 9, with support for both Docker and Podman container runtimes.

## Table of Contents

- [Quick Start](#quick-start)
- [Container Runtime Detection](#container-runtime-detection)
  - [Detection Implementation](#detection-implementation)
- [FIPS Compliance](#fips-compliance)
  - [What is FIPS 140-2?](#what-is-fips-140-2)
  - [Container Features](#container-features)
  - [Verification](#verification)
- [Runtime-Specific Differences](#runtime-specific-differences)
  - [Build Commands](#build-commands)
    - [User Namespace](#user-namespace)
  - [Socket Location](#socket-location)
  - [SELinux Support](#selinux-support)
- [Container Usage](#container-usage)
  - [Development Workflow](#development-workflow)
  - [Volume Mounts](#volume-mounts)
  - [User ID and Permissions](#user-id-and-permissions)
    - [Build-Time User ID Handling](#build-time-user-id-handling)
    - [Runtime User ID Handling](#runtime-user-id-handling)
    - [Custom User IDs](#custom-user-ids)
  - [Security Settings](#security-settings)
- [Makefile Commands](#makefile-commands)
- [Environment Variables](#environment-variables)
- [Building for Production](#building-for-production)
- [Troubleshooting](#troubleshooting)
  - [FIPS Mode Not Enabled](#fips-mode-not-enabled)
  - [Cryptography Errors](#cryptography-errors)
  - [Permission Issues](#permission-issues)
  - [Podman Socket Issues](#podman-socket-issues)
  - [SELinux Issues (Podman)](#selinux-issues-podman)
  - [Runtime Detection Issues](#runtime-detection-issues)
  - [Migration from Docker to Podman](#migration-from-docker-to-podman)
- [Performance Considerations](#performance-considerations)
- [Security Benefits](#security-benefits)
  - [Podman Advantages](#podman-advantages)
  - [Docker Advantages](#docker-advantages)
- [Integration with CI/CD](#integration-with-cicd)
- [Container Image Details](#container-image-details)
- [Notes](#notes)

## Quick Start

```bash
# Build the container image
make build

# Run lmer with Claude in a fresh container
make lmer

# Run tests locally
make test

# See all available commands
make help
```

## Container Runtime Detection

The system automatically detects which container runtime is available:

1. **Docker found**: Uses Docker (checked first)
2. **Podman found**: Uses Podman (fallback if Docker not found)
3. **Neither found**: Shows error message

**Note**: The current implementation checks for Docker first, then Podman. If both are installed, Docker will be used. This is a design choice prioritizing Docker's broader compatibility, though Podman offers better security with rootless operation.

### Detection Implementation

- **Makefile**: Uses `command -v docker 2>/dev/null || command -v podman`
- **Python (`src/lmer_cli/runtime.py`)**: Checks `shutil.which("docker")` first, then `shutil.which("podman")`
- **Shell scripts**: Check `command -v docker` then `command -v podman`

## FIPS Compliance

### What is FIPS 140-2?

Federal Information Processing Standard (FIPS) 140-2 is a U.S. government standard that defines minimum security requirements for cryptographic modules. When enabled:

- Only FIPS-approved cryptographic algorithms are available
- Non-approved algorithms (MD5, RC4, etc.) are disabled
- Cryptographic operations are validated

### Container Features

1. **Oracle Linux 9** - Enterprise-grade Linux with FIPS support
2. **FIPS Mode Enabled** - Cryptographic operations restricted to approved algorithms
3. **Python 3.11** - With FIPS-compliant OpenSSL bindings
4. **Security Hardening** - Runs as non-root user with minimal capabilities
5. **Resource Limits** - CPU, memory, and process limits to prevent fork bombs

### Verification

The container automatically verifies FIPS mode on startup:

```bash
# Check FIPS kernel mode
cat /proc/sys/crypto/fips_enabled  # Should output: 1

# Check OpenSSL FIPS
openssl version -a | grep -i fips

# Test Python crypto (inside container)
python3 -c "
import hashlib
# This works (FIPS-approved)
hashlib.sha256(b'test').hexdigest()
# This fails in strict FIPS mode
hashlib.md5(b'test').hexdigest()
"
```

## Runtime-Specific Differences

### Build Commands

**Docker**:
- Uses `--cache-from` for layer caching
- Build command: `docker build --cache-from <image> -t <image> -f Containerfile .`

**Podman**:
- Handles caching automatically (no `--cache-from` needed)
- Build command: `podman build -t <image> -f Containerfile .`

**Note**: Both the Makefile and `build.py` detect the runtime and only pass `--cache-from` when using Docker. Podman caches layers automatically.

### User Namespace

**Podman**:
- Adds `--userns=keep-id` flag when running containers (in `src/lmer_cli/runtime.py`)
- Maintains user ID mapping in rootless mode
- Preserves file permissions and SSH agent access

**Docker**:
- Runs without `--userns=keep-id`
- Uses standard user mapping

### Socket Location

**Docker**:
- Socket: `/var/run/docker.sock` (requires root or docker group membership)

**Podman**:
- Socket: `/run/user/<uid>/podman/podman.sock` (user-specific, rootless)

### SELinux Support

**Podman**:
- Uses `:Z` or `,z` suffix on volume mounts for SELinux private labeling
- Required on SELinux-enabled systems (RHEL, Fedora, etc.)
- Implemented in `src/lmer_cli/mounts.py` via `selinux_opt()` function

**Docker**:
- No SELinux labeling suffix needed
- SELinux support depends on Docker daemon configuration

## Container Usage

### Development Workflow

1. **Build the image**
   ```bash
   make build
   ```

2. **Run lmer (Claude inside a fresh container)**
   ```bash
   make lmer
   # or directly:
   ./lmer chat https://github.com/org/repo
   ```

3. **Get an interactive shell in a fresh container**
   ```bash
   make lmer-shell
   # or directly:
   ./lmer --no-clone --exec bash
   ```

4. **Use rule enforcement (inside the container)**
   ```bash
   rgr          # Read global rules
   rgrpc        # Run commit gate
   pc           # Commit with verification
   ```

5. **Run tests**
   ```bash
   make test       # Quick tests locally
   make test-all   # All tests locally
   make fips-check # Verify FIPS mode in the container
   ```

### Volume Mounts

- **Project Files**: `/home/developer/Agents/global` (live editing)
- **Workspace**: `/workspace` (current working directory)
- **Python venv**: Persisted in named volume `claude_venv`
- **Git Config**: Persisted in named volume `claude_git`
- **Claude Tracking**: Persisted in named volume `claude_tracking`
- **Global Rules**: `/Agents/global` (read-only safety rules)

### User ID and Permissions

The container is designed to match your host user ID to avoid permission issues when mounting volumes.

#### Build-Time User ID Handling

**Makefile Behavior**:
- Automatically detects your current user ID and group ID
- Passes them as build arguments: `BUILD_UID` and `BUILD_GID`
- Defaults to `$(id -u)` and `$(id -g)` if not explicitly set

**Containerfile Behavior**:
- Accepts `BUILD_UID` and `BUILD_GID` as build arguments
- Creates the `developer` user with matching IDs if provided:
  ```dockerfile
  RUN if [ -n "$BUILD_UID" ] && [ -n "$BUILD_GID" ]; then \
      groupadd -g "$BUILD_GID" developer && \
      useradd -m -s /bin/bash -u "$BUILD_UID" -g "$BUILD_GID" developer; \
  else \
      useradd -m -s /bin/bash developer; \
  fi
  ```
- Falls back to system-assigned UID/GID if build args are not provided

#### Runtime User ID Handling

**Docker**:
- Uses the user ID from the image (set at build time)
- File permissions depend on matching host and container UIDs
- If UIDs don't match, you may need to fix permissions:
  ```bash
  sudo chown -R $(id -u):$(id -g) /path/to/mount
  ```

**Podman**:
- Uses `--userns=keep-id` flag (in `src/lmer_cli/runtime.py`)
- Automatically maps your host UID/GID to the container user
- Works seamlessly even if container user has different IDs
- Better permission handling out of the box

#### Custom User IDs

To build with specific user IDs:

```bash
# Override user IDs during build
BUILD_UID=1000 BUILD_GID=1000 make build

# Or export them first
export BUILD_UID=1000
export BUILD_GID=1000
make build
```

**Use Cases**:
- Building on a system where your UID differs from the target system
- CI/CD environments with specific user requirements
- Multi-user systems where you need consistent IDs

### Security Settings

The container runs with:
- Non-root user (`developer`)
- Dropped capabilities (only essential ones retained: CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID)
- No new privileges flag (`--security-opt no-new-privileges`)
- Resource limits when running via the `lmer` CLI:
  - CPU: 1 core
  - Memory: 2GB
  - Process limit: 512

## Makefile Commands

```bash
# Core operations
make build          # Build container image
make build-nocache  # Build container image without cache
make rebuild        # Full rebuild from scratch (no cache)

# Run lmer
make lmer           # Run lmer with Claude in a fresh container
make lmer-shell     # Get a shell in a fresh lmer container

# Testing (local)
make test           # Run quick tests locally (excludes container build tests)
make test-all       # Run all tests locally

# Security
make fips-check     # Verify FIPS mode in the container
make security-scan  # Run security scan on the image

# Information
make help           # Show all available commands
make info           # Show environment information
make version        # Show Makefile version
```

## Environment Variables

Set via the host environment, `~/.lmer/.env`, or a project-local `.env` file:

- `OPENSSL_FIPS=1` - Enable FIPS mode in OpenSSL
- `PYTHONHASHSEED=0` - Deterministic hashing
- `CLAUDE_CONTAINER=true` - Container detection flag
- `CONTAINER_LIMITS=CPU:1core Memory:2GB Processes:512` - Resource limit info
- `GITLAB_TOKEN_*` - GitLab tokens (if needed)
- `CLAUDE_API_KEY` - Claude API key (if needed)

## Building for Production

For production deployments:

```dockerfile
# Add to Containerfile
RUN pip install --no-cache-dir -r fips-requirements.txt && \
    pip freeze > /tmp/locked-requirements.txt

# Security scanning
RUN dnf install -y openscap-scanner && \
    oscap xccdf eval --profile xccdf_org.ssgproject.content_profile_stig \
    /usr/share/xml/scap/ssg/content/ssg-ol9-ds.xml || true
```

## Troubleshooting

### FIPS Mode Not Enabled

If FIPS mode isn't enabled:
```bash
# Inside container as root
sudo fips-mode-setup --enable
# Restart container
```

### Cryptography Errors

If you see "ValueError: error:0308010C:digital envelope routines::unsupported":
- The algorithm isn't FIPS-approved
- Use SHA-256/SHA-512 instead of MD5/SHA-1
- Check `openssl list -cipher-algorithms` for approved ciphers

### Permission Issues

**Docker**:
```bash
# Fix ownership from host
sudo chown -R $(id -u):$(id -g) .

# Or rebuild with matching user IDs (see User ID and Permissions section)
make build
```

**Podman**:
```bash
# Podman handles permissions automatically with --userns=keep-id
# If issues persist, check SELinux contexts:
ls -Z /path/to/mount
# Fix SELinux context if needed:
chcon -Rt container_file_t /path/to/mount
```

**Note**: See the "User ID and Permissions" section above for details on how user IDs are handled at build and runtime.

### Podman Socket Issues

If you encounter issues with the podman socket (e.g. when using service mode):

1. Ensure the podman socket is running:
   ```bash
   systemctl --user start podman.socket
   ```

2. Check the socket path:
   ```bash
   podman info --format '{{.Host.RemoteSocket.Path}}'
   ```

3. Verify the socket exists:
   ```bash
   ls -l /run/user/$(id -u)/podman/podman.sock
   ```

### SELinux Issues (Podman)

Podman applies SELinux labels (`:Z`/`,z`) to bind mounts automatically. If you encounter permission issues:

1. Check SELinux status:
   ```bash
   sestatus
   ```

2. Check volume context:
   ```bash
   ls -Z /path/to/mounted/volume
   ```

3. Temporarily disable SELinux enforcement (not recommended for production):
   ```bash
   sudo setenforce 0
   ```

4. Fix SELinux context:
   ```bash
   chcon -Rt container_file_t /path/to/mount
   ```

### Runtime Detection Issues

If the wrong runtime is detected:

1. Check what's available:
   ```bash
   which docker podman
   ```

2. Force a specific runtime by modifying PATH or using full paths:
   ```bash
   # Use Podman explicitly
   /usr/bin/podman run --rm lmer ...
   ```

### Migration from Docker to Podman

1. Export images:
   ```bash
   docker save lmer > image.tar
   ```

2. Import to Podman:
   ```bash
   podman load < image.tar
   ```

3. Update any Docker-specific configurations
4. Run lmer with podman explicitly if needed:
   ```bash
   podman run --rm -it lmer
   ```

## Performance Considerations

- **Podman**: Rootless by default, which may have slight performance overhead but provides better security isolation
- **Docker**: Requires daemon with root privileges, generally faster but less secure
- Both support resource limits (CPU, memory, PIDs)
- Both support layer caching for faster builds

## Security Benefits

### Podman Advantages

1. **Rootless**: Runs without root daemon
2. **No daemon**: Each container is a child process
3. **systemd integration**: Better process management
4. **SELinux**: Enhanced security contexts by default
5. **User namespace**: Better isolation by default

### Docker Advantages

1. **Mature ecosystem**: More third-party tools and integrations
2. **Performance**: Slightly faster in some scenarios
3. **Compatibility**: Better compatibility with older systems

## Integration with CI/CD

```yaml
# .gitlab-ci.yml example
test:
  image: lmer
  script:
    - source .venv/bin/activate
    - pytest tests/ --cov=. --cov-report=xml
    - pre-commit run --all-files
```

## Container Image Details

- **Base Image**: `oraclelinux:9-slim-fips`
- **Image Name**: `lmer`
- **Container Name**: ephemeral (named per-run by `lmer`)
- **User**: `developer` (non-root)
- **Working Directory**: `/workspace` (default), `/home/developer/Agents/global` (development)

## Notes

- Container includes all enforcement hooks
- Git operations work within container
- Pre-commit hooks are installed automatically
- All rule modules are available
- Tests run in FIPS-compliant environment
- LMER (LLM Environment Runtime) provides safe execution with resource limits
- Container automatically detects and mounts global rules for safety checks
