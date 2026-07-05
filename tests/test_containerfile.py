"""Tests for Containerfile configuration."""
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


class TestBuildProvenanceBaked:
    def test_containerfile_bakes_build_commit(self):
        content = (Path(__file__).parent.parent / "Containerfile").read_text()
        assert "ARG LMER_BUILD_COMMIT" in content
        assert "ENV LMER_BUILD_COMMIT" in content
        assert "/Agents/global/BUILD_INFO" in content
