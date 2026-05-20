# Dependency Management Rules

## 🚨 Critical Dependency Rules
- **ALWAYS** commit lock files (uv.lock, package-lock.json, poetry.lock, etc.)
- **ALWAYS** run security audits on dependencies
- **NEVER** add dependencies without checking if they already exist
- **NEVER** ignore security vulnerabilities

## Lock Files
- Always commit lock files (package-lock.json, uv.lock, poetry.lock, etc.)
- Update lock files when adding/updating dependencies
- Use exact versions in lock files

## Adding Dependencies
- Check package.json/requirements.txt before adding new dependencies
- Pin dependency versions for reproducibility
- Document why each dependency is needed
- Prefer well-maintained packages
- Check license compatibility

## Dependency Maintenance
- Review security advisories for dependencies
- Keep dependencies up to date
- Remove unused dependencies
- Use tools like `npm audit` or `pip-audit`
- Update dependencies separately from feature work

## Development Dependencies
- Separate dev dependencies from production
- Don't install dev dependencies in production
- Keep test/lint tools as dev dependencies
