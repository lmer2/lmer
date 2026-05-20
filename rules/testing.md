# Testing & Quality Assurance Rules

## 🚨 Critical
- ALWAYS write unit tests, do not do 1 off shell scripts
- ALWAYS make unit tests for fixing bugs
- NEVER run tests without understanding what they do first
- ALWAYS check test code for resource exhaustion risks before running

## 🛑 Pre-Commit Testing Checklist
**STOP before any commit and verify:**
- [ ] All unit tests written (not shell scripts)
- [ ] Ran `python -m pytest tests/ -x` - ALL PASS
- [ ] Ran `pre-commit run --all-files` - ALL PASS
- [ ] User has reviewed changes
- [ ] User explicitly approved commit

**If ANY check fails: DO NOT COMMIT**

## Test Framework
- Always use pytest (never unittest)
- Add tests for all fixes unless extensive mocking required
- ALWAYS check that ALL tests pass before declaring anything complete
- If tests fail, iterate until fixed
- Profile before optimizing performance
- Use conftest.py for shared test configuration
- Define global fixtures in conftest.py where possible
- Track all environment variables in a `clean_env` fixture in conftest.py
- Do not prefix pytest fixtures with underscore (_)

## Fixture Best Practices
- Convert repeated monkeypatch operations into fixtures
- Do not pass monkeypatch as parameter when it should be a fixture
- Use `autouse=True` for fixtures that should run for all tests (e.g., skip_version_check)
- For opt-out of autouse fixtures, use pytest marks (e.g., `@pytest.mark.no_skip_version_check`)
- Check for marks in the fixture using `if "mark_name" not in request.keywords:`

## Test Coverage
- Add test coverage measurement to all projects (pytest-cov)
- Aim for minimum 80% code coverage for new features
- Evaluate and report coverage metrics before declaring PR complete
- Add coverage reports to CI/CD pipelines
- Exclude only truly untestable code (with justification)

## Test Data
- Prefer file-based test data over inline data in test code
- Store test data files in `tests/data/$identifier/` directories
- Use meaningful identifiers that describe the test scenario
- Keep test data organized and versioned with the code

## Test Organization
- One test file per module
- Use descriptive test names that explain what is being tested
- Follow AAA pattern: Arrange, Act, Assert

## Test Safety Rules
- **ALWAYS review test code before running** - understand what each test does
- **Check for resource exhaustion risks**:
  - Unbounded loops or recursion
  - Large memory allocations
  - Subprocess calls without timeouts
  - File system operations that could fill disk
  - Network operations without timeouts
- **Add resource limits to tests**:
  - Use `timeout` parameter on subprocess calls (max 30s)
  - Limit iterations in performance tests
  - Mock expensive operations instead of running them
  - Use tmp_path fixture for file operations
- **NEVER run unfamiliar test suites** without reviewing them first
- **If a test causes problems** (fork bomb, memory exhaustion, etc):
  - Kill the process immediately
  - Review the test code
  - Add safety limits before re-running
  - Document the issue in test docstring
