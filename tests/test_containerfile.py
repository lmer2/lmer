"""Tests for Containerfile configuration."""
import tomllib

import pytest
from pathlib import Path


class TestContainerfile:
    """Test Containerfile structure and content."""

    def test_containerfile_exists(self, project_root):
        """Verify Containerfile exists."""
        containerfile = project_root / "Containerfile"
        assert containerfile.exists(), "Containerfile not found"
        assert containerfile.is_file(), "Containerfile is not a file"

    def test_containerfile_base_image(self, project_root):
        """Verify correct base image is used."""
        containerfile = project_root / "Containerfile"
        content = containerfile.read_text()
        assert "FROM oraclelinux:9-slim-fips" in content, "Base image should be Oracle Linux 9-slim-fips"

    def test_containerfile_fips_enabled(self, project_root):
        """Verify FIPS mode is configured."""
        containerfile = project_root / "Containerfile"
        content = containerfile.read_text()

        # Check FIPS setup - 9-slim-fips base image has FIPS pre-configured
        assert "9-slim-fips" in content, "Not using FIPS-enabled base image"
        assert "OPENSSL_FIPS=1" in content, "OPENSSL_FIPS environment variable not set"

    def test_containerfile_non_root_user(self, project_root):
        """Verify container runs as non-root user."""
        containerfile = project_root / "Containerfile"
        content = containerfile.read_text()

        # Check user creation and switch
        assert "useradd" in content, "Non-root user not created"
        assert "USER developer" in content, "Container not switched to non-root user"
        assert "WORKDIR /Agents/global" in content, "Working directory not set for user"

    def test_containerfile_python_setup(self, project_root):
        """Verify Python environment is properly configured."""
        containerfile = project_root / "Containerfile"
        content = containerfile.read_text()

        # Check Python installation
        assert "python3" in content.lower(), "Python not installed"
        assert "uv" in content, "uv package manager not installed"
        assert "uv venv" in content, "Virtual environment not created"
        assert "uv sync" in content, "Dependencies not synchronized"

    def test_containerfile_security_settings(self, project_root):
        """Verify security settings in Containerfile."""
        containerfile = project_root / "Containerfile"
        content = containerfile.read_text()

        # Security checks
        assert "PYTHONHASHSEED=0" in content, "Python hash seed not set"
        assert "/etc/sudoers" in content, "Sudo configuration missing"

        # Check for proper file ownership
        assert "--chown=developer:developer" in content, "File ownership not properly set"

    def test_containerfile_entrypoint(self, project_root):
        """Verify entrypoint configuration."""
        containerfile = project_root / "Containerfile"
        content = containerfile.read_text()

        assert "ENTRYPOINT" in content, "No ENTRYPOINT defined"
        assert "entrypoint.sh" in content, "Entrypoint script not configured"
        assert "CMD" in content, "No default CMD defined"

    def test_containerfile_hooks_installation(self, project_root):
        """Verify hooks setup is documented in container."""
        containerfile = project_root / "Containerfile"
        content = containerfile.read_text()

        # Pre-commit is now installed at runtime when in a git repo
        assert "pre-commit" in content or "Note: pre-commit" in content, "No mention of pre-commit hooks"
        # Custom hooks can be installed via the Makefile target
        assert "entrypoint.sh" in content, "Entrypoint script not configured"

    def test_dockerignore_updated(self, project_root):
        """Verify .dockerignore references Containerfile."""
        dockerignore = project_root / ".dockerignore"
        if dockerignore.exists():
            content = dockerignore.read_text()
            assert "Containerfile" in content or "Dockerfile" not in content, \
                ".dockerignore should reference Containerfile, not Dockerfile"

    def test_no_dockerfile_references(self, project_root):
        """Verify no remaining references to Dockerfile."""
        # Files that should not reference Dockerfile
        files_to_check = [
            project_root / "README.md",
            project_root / "DOCKER.md",
            project_root / "tests" / "test_security.py",
            project_root / "tests" / "test_integration.py"
        ]

        for file_path in files_to_check:
            if file_path.exists():
                content = file_path.read_text()
                # Allow Dockerfile in comments or documentation about migration
                lines_with_dockerfile = [
                    line for line in content.split('\n')
                    if 'Dockerfile' in line and 'Containerfile' not in line
                ]
                for line in lines_with_dockerfile:
                    # Skip if it's a comment about the rename
                    if any(word in line.lower() for word in ['renamed', 'moved', 'was', 'legacy']):
                        continue
                    assert False, f"{file_path} still references Dockerfile: {line}"


class TestPyYamlEntrypointGuarantee:
    """The bare `python3` the host CLI invokes in-container must have PyYAML.

    The Containerfile guarantees this explicitly (commented build gate), not
    as an accident of PATH ordering. The in-image import check itself lives in
    the doctor container-gated tests; here we only assert the Containerfile
    carries the guarantee.
    """

    def test_containerfile_has_commented_pyyaml_guarantee(self, project_root):
        content = (project_root / "Containerfile").read_text()
        assert "PyYAML guarantee" in content, \
            "Containerfile missing the commented PyYAML guarantee"

    def test_containerfile_asserts_python3_is_venv(self, project_root):
        """The build gate pins bare `python3` to the uv-synced venv."""
        content = (project_root / "Containerfile").read_text()
        assert '[ "$(command -v python3)" = "/Agents/global/.venv/bin/python3" ]' in content, \
            "Containerfile build gate does not assert python3 resolves to the venv"
        assert "import sys, yaml" in content, \
            "Containerfile build gate does not import yaml"

    def test_guarantee_runs_after_path_env_and_final_sync(self, project_root):
        """The gate must validate the final state: after ENV PATH and uv sync."""
        content = (project_root / "Containerfile").read_text()
        path_env = content.index('ENV PATH="/opt/tools/bin')
        final_sync = content.rindex("uv sync")
        gate = content.index("PyYAML guarantee")
        assert path_env < gate, "PyYAML build gate must come after the ENV PATH line"
        assert final_sync < gate, "PyYAML build gate must come after the final uv sync"

    def test_venv_path_precedes_system_bin(self, project_root):
        """/Agents/global/.venv/bin must appear in ENV PATH (ahead of /usr/bin)."""
        content = (project_root / "Containerfile").read_text()
        env_line = next(
            line for line in content.splitlines()
            if line.startswith('ENV PATH=')
        )
        assert "/Agents/global/.venv/bin" in env_line, \
            "venv bin dir missing from ENV PATH"
        # ${PATH} (which holds /usr/bin) is appended last, so venv wins
        assert env_line.rstrip('"').endswith("${PATH}"), \
            "ENV PATH must append the inherited PATH last so the venv wins"

    def test_pyproject_declares_pyyaml(self, project_root):
        """The venv only carries PyYAML because pyproject.toml requires it."""
        content = (project_root / "pyproject.toml").read_text()
        assert "pyyaml>=6.0" in content, "pyproject.toml no longer requires pyyaml>=6.0"


class TestContainerLimitsDerived:
    """CONTAINER_LIMITS comes from the cgroup at shell startup, not from a literal.

    CPU, memory and pids limits are per-run settings, so any value written into
    ~/.bashrc at build time is wrong for every run that overrides one.
    """

    def test_no_baked_container_limits_literal(self, project_root):
        content = (project_root / "Containerfile").read_text()
        assert 'CONTAINER_LIMITS="CPU:' not in content, \
            "Containerfile still bakes a literal CONTAINER_LIMITS value into ~/.bashrc"

    def test_bashrc_sources_container_limits_script(self, project_root):
        content = (project_root / "Containerfile").read_text()
        assert ". /home/developer/container-limits.sh /sys/fs/cgroup' >> ~/.bashrc" in content, \
            "~/.bashrc does not source container-limits.sh with an explicit cgroup root"

    def test_container_limits_script_is_copied_and_executable(self, project_root):
        content = (project_root / "Containerfile").read_text()
        assert (
            "COPY --chown=developer:developer Ctl/container/container-limits.sh "
            "/home/developer/container-limits.sh"
        ) in content, "container-limits.sh is not copied into the image"
        chmod_line = next(
            line for line in content.splitlines()
            if line.startswith("RUN chmod +x /home/developer/entrypoint.sh")
        )
        assert "/home/developer/container-limits.sh" in chmod_line, \
            "container-limits.sh is not chmod +x'd alongside the entrypoint"

    def test_script_exists_in_repo(self, project_root):
        script = project_root / "Ctl" / "container" / "container-limits.sh"
        assert script.is_file(), "Ctl/container/container-limits.sh not found"


class TestBuildProvenanceBaked:
    def test_containerfile_bakes_build_commit(self):
        content = (Path(__file__).parent.parent / "Containerfile").read_text()
        assert "ARG LMER_BUILD_COMMIT" in content
        assert "ENV LMER_BUILD_COMMIT" in content
        assert "/Agents/global/BUILD_INFO" in content


class TestCodexManagedHooks:
    """lmer's hook is trusted without trusting arbitrary repository hooks."""

    def test_image_installs_system_requirements(self, project_root):
        content = (project_root / "Containerfile").read_text()
        assert (
            "COPY --chown=root:root agent-files/codex/requirements.toml "
            "/etc/codex/requirements.toml"
            in content
        )
        assert content.index("/etc/codex/requirements.toml") < content.index(
            "USER developer"
        ), "system requirements must be installed while the image is still root"

    def test_requirements_enable_only_the_lmer_ask_guard(self, project_root):
        path = project_root / "agent-files" / "codex" / "requirements.toml"
        requirements = tomllib.loads(path.read_text())
        assert requirements["feature_requirements"]["hooks"] is True, (
            "managed Stop hooks must not be disabled by a lower config layer"
        )
        assert requirements["hooks"]["managed_dir"] == "/Agents/global/hooks"
        stop = requirements["hooks"]["Stop"]
        assert len(stop) == 1
        assert stop[0]["hooks"] == [
            {
                "type": "command",
                "command": "python3 /Agents/global/hooks/codex_ask_guard.py",
                "timeout": 3600,
                "statusMessage": "Waiting for the operator's lmer-ask answer",
            }
        ]
        assert "allow_managed_hooks_only" not in requirements, (
            "user and project hooks must keep their ordinary trust path"
        )
