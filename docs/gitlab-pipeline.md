# gitlab-pipeline

Monitor and debug GitLab CI/CD pipelines from the command line.

## Installation

The script is available at `bin/gitlab-pipeline` and requires no additional dependencies beyond Python 3.

## Usage

```bash
gitlab-pipeline <project> <id> [options]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `project` | Project path (e.g., `agents/global`, `myorg/myrepo`) |
| `id` | Pipeline ID, MR ID (with `--mr`), or Job ID (with `--trace`) |

### Options

| Option | Description |
|--------|-------------|
| `--mr` | Treat ID as a merge request ID (fetches its pipeline) |
| `--watch`, `-w` | Poll until pipeline completes, show failed job traces |
| `--trace`, `-t` | Get job trace/log (ID is interpreted as job ID) |
| `--host HOST` | GitLab host (from `$GITLAB_HOST` or specified explicitly) |

## Examples

### Check pipeline status
```bash
# Direct pipeline ID
gitlab-pipeline agents/global 230

# From merge request
gitlab-pipeline agents/global 9 --mr
```

Output:
```
MR 9 -> Pipeline 230
Pipeline 230: running
URL: https://gitlab.example.com/myorg/myrepo/-/pipelines/230

Jobs:
  ✅ pre-commit           success      lint
  🔄 pytest               running      test
```

### Watch pipeline until complete
```bash
gitlab-pipeline agents/global 9 --mr --watch
```

Polls every 10 seconds. When pipeline fails, automatically shows the last 50 lines of each failed job's trace.

### Get job trace
```bash
# Get full trace for job 924
gitlab-pipeline agents/global 924 --trace
```

### Different GitLab instance
```bash
gitlab-pipeline myorg/myrepo 123 --host gitlab.example.com --mr
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GITLAB_HOST` | Default GitLab host |
| `GITLAB_TOKEN_{sanitized_host}` | Host-specific API token (hostname with dots/hyphens as underscores) |
| `GITLAB_TOKEN` | Fallback API token for any host |

### Token Requirements

The API token needs these scopes:
- `read_api` - Read access to API (pipelines, jobs, traces)

## Status Icons

| Icon | Status |
|------|--------|
| ✅ | success |
| ❌ | failed |
| 🔄 | running |
| ⏳ | pending |
| ⏭️ | skipped |
| 🚫 | canceled |
| 👆 | manual |

## Technical Notes

### Job Trace API

The GitLab job trace endpoint requires `Accept: text/plain` header. Without it, the API returns 401 Unauthorized even with valid credentials. This script handles this automatically.

```
# This fails:
curl -H "PRIVATE-TOKEN: $TOKEN" .../jobs/123/trace

# This works:
curl -H "PRIVATE-TOKEN: $TOKEN" -H "Accept: text/plain" .../jobs/123/trace
```

### ANSI Stripping

Job traces contain ANSI escape codes for terminal colors. The script strips these for cleaner output when displaying traces.
