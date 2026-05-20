# CI/CD & Monitoring Rules

## 🚨 Critical CI/CD Rules
- **ALWAYS** actually wait and monitor when checking CI/CD status
- **ALWAYS** wait for tests to pass before declaring PR complete

## 🛑 PR GATE - BEFORE DECLARING "READY"
**STOP and verify EVERY item:**
- [ ] All CI checks green (actually waited and verified)
- [ ] Coverage report shows >80%
- [ ] No security vulnerabilities reported
- [ ] User reviewed all changes
- [ ] STOP: Show user the CI status screenshot/output

**If ANY check fails: DO NOT declare PR ready**

## GitHub Actions Monitoring
- ⚠️ Actually wait between status checks (don't just say "checking")
- Monitor progress with appropriate delays between checks
- Fix failures and continue monitoring new runs
- Never declare "ready" without verifying final results
- Document any CI/CD configuration changes

## Verification Process
- Use sleep commands to wait between checks
- Continue monitoring until process completes
- Don't declare success without seeing the actual results
- Wait for all checks to finish before proceeding
- Check logs when builds fail
- Verify deployment success in target environment

## CI/CD Best Practices
- Keep CI runs under 10 minutes
- Cache dependencies appropriately
- Run tests in parallel when possible
- Fail fast on critical errors
- Include security scanning
- Add coverage reports
