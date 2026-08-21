"""``--service-group`` on the lmer CLI (issue #312).

A group attaches one session to every running member of a compose project, so
that a 12-container stack needs one lmer instance rather than twelve. This file
covers the host-side half: the flag's rules, what it resolves at launch, and
what reaches the container — the switching itself is
``tests/test_target_switch.py``.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.cli import _validate_parsed_args, parse_args, resolve_service_target
from lmer_cli.service import ServiceError, ServiceMember


CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


def _args(*tokens):
    ns, _ = parse_args(["chat", *tokens], quiet=True)
    return ns


class TestFlagRules:
    """The rules that hold before anything is resolved."""

    def test_group_requires_checkout(self, capsys):
        """Same rule as --service, and for the same reason: nothing to edit."""
        assert _validate_parsed_args(_args("--service-group", "stack")) == 2
        assert "--service-group requires --checkout" in capsys.readouterr().out

    def test_group_with_checkout_is_accepted(self):
        assert _validate_parsed_args(_args("--service-group", "stack",
                                   "--checkout", "/tmp")) is None

    def test_group_and_service_compose(self):
        """--service names the starting member; it does not conflict."""
        ns = _args("--service-group", "stack", "--service", "web",
                   "--checkout", "/tmp")
        assert _validate_parsed_args(ns) is None
        assert (ns.service_group, ns.service) == ("stack", "web")


class TestContainerEnv:
    """Guards on cli.py's container env dict.

    Source-level, like the other passthrough guards in tests/test_gates.py: a
    variable dropped from this dict has no effect inside the container, which is
    the only place ``target-switch`` and ``target-exec`` read it.
    """

    def test_env_dict_declares_service_group(self):
        assert re.search(
            r"""["']LMER_SERVICE_GROUP["']\s*:\s*ns\.service_group""",
            CLI_PY.read_text(),
        ), "LMER_SERVICE_GROUP missing from cli.py container env dict"

    def test_env_dict_declares_target_file(self):
        assert re.search(
            r"""["']LMER_SERVICE_TARGET_FILE["']\s*:""",
            CLI_PY.read_text(),
        ), "LMER_SERVICE_TARGET_FILE missing from cli.py container env dict"

    def test_target_file_default_is_shared_with_the_switcher(self):
        """The CLI must publish the path target-switch actually writes."""
        from lmer_cli.cli import CONTAINER_SERVICE_TARGET_FILE
        from lmer_cli.container.target_switch import DEFAULT_TARGET_FILE

        assert CONTAINER_SERVICE_TARGET_FILE == DEFAULT_TARGET_FILE


class TestStartingMember:
    """What a launch resolves, through the CLI's own seam."""

    MEMBERS = [
        ServiceMember("db", "bbb222", "stack-db-1", "/"),
        ServiceMember("web", "aaa111", "stack-web-1", "/srv/app"),
    ]

    def test_service_inside_a_group_supplies_the_starting_container(self):
        with patch("lmer_cli.cli.resolve_group", return_value=self.MEMBERS):
            target = resolve_service_target("docker", "web", "stack")
        assert target == ("aaa111", "/srv/app", "web")

    def test_group_alone_starts_targetless(self):
        """No invented default: the agent picks, and nothing is picked for it."""
        with patch("lmer_cli.cli.resolve_group", return_value=self.MEMBERS):
            assert resolve_service_target("docker", None, "stack") == (
                None, None, None,
            )

    def test_non_member_is_refused_with_the_member_list(self):
        with patch("lmer_cli.cli.resolve_group", return_value=self.MEMBERS):
            with pytest.raises(ServiceError) as exc:
                resolve_service_target("docker", "worker", "stack")
        message = str(exc.value)
        assert "worker" in message and "web" in message and "db" in message

    def test_a_group_is_resolved_even_when_targetless(self):
        """A project the host cannot see must fail at launch, not at first use."""
        with patch(
            "lmer_cli.cli.resolve_group",
            side_effect=ServiceError("No running containers in compose project"),
        ):
            with pytest.raises(ServiceError):
                resolve_service_target("docker", None, "stack")

    def test_single_service_mode_is_untouched(self):
        """Without a group, resolution is exactly the pre-#312 pair of calls."""
        with patch(
            "lmer_cli.cli.resolve_container", return_value="ccc333"
        ) as resolve, patch(
            "lmer_cli.cli.inspect_container_workdir", return_value="/app"
        ):
            assert resolve_service_target("docker", "web", None) == (
                "ccc333", "/app", "web",
            )
        resolve.assert_called_once_with("docker", "web")
