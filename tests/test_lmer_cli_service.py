"""Tests for lmer_cli.service module."""

from unittest.mock import MagicMock, patch

import pytest

from lmer_cli.service import (
    ServiceError,
    inspect_container_workdir,
    resolve_container,
)


def _ps_result(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    """Helper: build a MagicMock subprocess result."""
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestResolveContainer:
    """Tests for resolve_container."""

    def test_label_match_wins_first(self):
        """Compose label match short-circuits before name matching."""
        label = _ps_result("abc123def456\tmyappdev-myapp-1\n")
        with patch("lmer_cli.service.subprocess.run", side_effect=[label]) as m:
            cid = resolve_container("docker", "myapp")
        assert cid == "abc123def456"
        first_args = m.call_args_list[0].args[0]
        assert any(
            a == "label=com.docker.compose.service=myapp" for a in first_args
        )

    def test_falls_back_to_exact_name_when_no_label_match(self):
        """When no compose-label container exists, exact name match is used."""
        no_label = _ps_result("")
        by_name = _ps_result("abc123def456\tmy-service\n")
        with patch(
            "lmer_cli.service.subprocess.run", side_effect=[no_label, by_name]
        ):
            cid = resolve_container("docker", "my-service")
        assert cid == "abc123def456"

    def test_substring_match_is_ignored(self):
        """Substring-collision regression: with --service myapp and no compose
        label, docker's substring filter returns both myappdev-database-1
        and myappdev-myapp-1; neither matches exactly, so we error
        rather than pick one."""
        no_label = _ps_result("")
        # Docker returns substring matches; neither name equals 'myapp'.
        by_name = _ps_result(
            "bfa20c97f890\tmyappdev-database-1\n"
            "06f9080e172f\tmyappdev-myapp-1\n"
        )
        running = _ps_result(
            "myappdev-database-1\nmyappdev-myapp-1\n"
        )
        with patch(
            "lmer_cli.service.subprocess.run",
            side_effect=[no_label, by_name, running],
        ):
            with pytest.raises(ServiceError, match="No running container matched"):
                resolve_container("docker", "myapp")

    def test_label_match_with_multiple_containers_errors(self):
        """If the compose service has scaled replicas, refuse to pick one."""
        label = _ps_result(
            "aaaaaaaaaaaa\tproj-svc-1\nbbbbbbbbbbbb\tproj-svc-2\n"
        )
        with patch("lmer_cli.service.subprocess.run", side_effect=[label]):
            with pytest.raises(
                ServiceError, match="Multiple containers share compose service"
            ):
                resolve_container("docker", "svc")

    def test_raises_on_no_match(self):
        no_label = _ps_result("")
        no_name = _ps_result("")
        running = _ps_result("other-container\n")
        with patch(
            "lmer_cli.service.subprocess.run",
            side_effect=[no_label, no_name, running],
        ):
            with pytest.raises(ServiceError, match="No running container matched"):
                resolve_container("docker", "nonexistent")

    def test_raises_on_runtime_failure(self):
        with patch(
            "lmer_cli.service.subprocess.run",
            side_effect=FileNotFoundError("not found"),
        ):
            with pytest.raises(ServiceError, match="Failed to query"):
                resolve_container("docker", "svc")

    def test_raises_on_nonzero_return(self):
        err = _ps_result("", returncode=1, stderr="permission denied")
        with patch("lmer_cli.service.subprocess.run", return_value=err):
            with pytest.raises(ServiceError, match="permission denied"):
                resolve_container("docker", "svc")


class TestInspectContainerWorkdir:
    """Tests for inspect_container_workdir."""

    def test_returns_workdir(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "/app\n"
        with patch("lmer_cli.service.subprocess.run", return_value=mock_result):
            assert inspect_container_workdir("docker", "abc123") == "/app"

    def test_returns_fallback_on_empty(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        with patch("lmer_cli.service.subprocess.run", return_value=mock_result):
            assert inspect_container_workdir("docker", "abc123") == "/"

    def test_returns_fallback_on_error(self):
        with patch("lmer_cli.service.subprocess.run", side_effect=Exception("fail")):
            assert inspect_container_workdir("docker", "abc123") == "/"
