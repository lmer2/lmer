# Security Rules

## 🚨 Critical Security Rules
- **NEVER** expose API keys, tokens, or secrets in code or logs
- **NEVER** commit credentials to the repository

## Secrets Management
- Always use environment variables for sensitive configuration
- Follow principle of least privilege for permissions
- Validate all external inputs
- Store secrets in appropriate secret management systems
- Use .env files locally (never commit them)

## Security Checklist
- [ ] No hardcoded secrets
- [ ] Environment variables for sensitive data
- [ ] Input validation implemented
- [ ] No sensitive data in logs
- [ ] SQL injection prevention
- [ ] XSS prevention for web apps
- [ ] CSRF protection enabled

## Dependency Security
- Review security advisories for dependencies
- Keep dependencies up to date
- Use tools like `pip-audit` or `safety` for Python
- Pin dependency versions for reproducibility
- Audit new dependencies before adding
