# Code Quality & Organization Rules

## 🚨 Critical Code Quality Rules
- **ALWAYS** run pre-commit before committing
- **ALWAYS** run linters before committing
- **NEVER** commit code with print statements (use logging)
- **NEVER** put business logic in CLI modules

## Pre-commit Hooks
- Always add and configure pre-commit for whitespace and linting
- Always run `pre-commit install` to install git hooks
- Verify hook installation - commits should fail without pre-commit checks
- Run pre-commit before any commits
- Fix all pre-commit issues before proceeding

## Code Quality Standards
- Always run linters before committing
- Follow existing code style in the project
- Keep functions focused and under 50 lines when possible
- Document performance-critical sections
- Use proper logging instead of print statements

## Code Organization
- CLI modules should contain only CLI-relevant logic
- Move all business logic to library modules
- Keep CLI modules thin - parsing arguments and calling library functions
- Separate concerns: CLI handles I/O, libraries handle logic

## File Management
- Use `LOCAL/tmp/` for temporary files
- Prefer editing existing files over creating new ones
- Write documentation for new features and significant changes
- Clean up temporary files when done

## Code Style
- Use descriptive variable and function names
- Maintain consistent indentation
- Follow PEP 8 for Python code
- Use type hints where applicable
- Avoid deep nesting (max 3 levels)
