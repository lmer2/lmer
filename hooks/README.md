# Rule Enforcement Hooks

Automated enforcement for development shortcuts that ensure rules are followed.

## Installation

```bash
./hooks/install.sh
```

This will:
- Install command wrappers in `~/.local/bin`
- Set up git hooks for commit/push enforcement
- Create tracking directories in `~/.claude`

## Commands

### Basic Commands

- `rgr` - Read Global Rules with tracking
- `rgrpc` - Read rules and run COMMIT GATE checks
- `pc` - Please Commit with gate verification

### Focused Commands

- `rgr-git` - Git & version control rules only
- `rgr-test` - Testing rules only
- `rgr-code` - Code quality rules only
- `rgr-security` - Security rules only
- `rgr-docs` - Documentation rules only
- `rgr-ci` - CI/CD rules only
- `rgr-deps` - Dependency rules only

## How It Works

### Rule Reading Tracking
When you run `rgr` or any focused command, the hook:
1. Verifies rule files exist
2. Records timestamp of reading
3. Shows recent violations
4. Highlights critical sections

### Commit Gate Enforcement
The `rgrpc` command:
1. Checks rules were read recently
2. Runs tests automatically
3. Runs pre-commit checks
4. Shows all results
5. Records gate passage

### Commit Verification
The `pc` command:
1. Verifies COMMIT GATE was passed (<1 hour ago)
2. Checks for staged changes
3. Scans for potential secrets
4. Creates commit with your message
5. Offers to push if repo is on allow list

## Git Hook Integration

### Pre-commit Hook
- Verifies COMMIT GATE was passed recently
- Runs pre-commit if installed
- Blocks commits without gate passage

### Pre-push Hook
- Checks repository against allow list
- Blocks pushes to unauthorized repos
- Shows clear error messages

## Tracking Files

Located in `~/.claude/`:
- `last_rgr_timestamp` - When rules were last read
- `gate_passages.log` - COMMIT GATE passages
- `commits.log` - Commits made via pc
- `violations.log` - Rule violations (if any)

## Testing

```bash
python -m pytest tests/test_hooks.py -v
```

## Customization

Edit the hooks to add:
- Additional validation rules
- Custom gate checks
- Integration with your tools
- Violation reporting
