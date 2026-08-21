"""Tests for lmer_cli.service module."""

from unittest.mock import MagicMock, patch

import pytest

from lmer_cli.service import (
    ServiceError,
    ServiceMember,
    describe_members,
    inspect_container_workdir,
    member_names,
    member_of,
    resolve_container,
    resolve_group,
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


def _inspect_result(rows: list[tuple[str, str, str, str]]) -> MagicMock:
    """Helper: an `inspect --format` result, one tab-joined row per container."""
    return _ps_result(
        "".join("\t".join(row) + "\n" for row in rows)
    )


class TestResolveGroup:
    """Tests for resolve_group — the compose project *is* the group (#312)."""

    def test_members_come_from_the_project_label(self):
        """One ps by project label, one inspect over every id it returned."""
        ps = _ps_result("aaa111\tstack-web-1\nbbb222\tstack-db-1\n")
        inspect = _inspect_result([
            ("aaa111", "/stack-web-1", "/srv/app",
             '{"com.docker.compose.service":"web",'
             '"com.docker.compose.project":"stack"}'),
            ("bbb222", "/stack-db-1", "/",
             '{"com.docker.compose.service":"db",'
             '"com.docker.compose.project":"stack"}'),
        ])
        with patch(
            "lmer_cli.service.subprocess.run", side_effect=[ps, inspect]
        ) as m:
            members = resolve_group("docker", "stack", announce=False)

        assert [m_.service for m_ in members] == ["db", "web"]  # sorted by service
        assert members[1].container_id == "aaa111"
        assert members[1].container_name == "stack-web-1"  # docker's slash stripped
        assert members[1].workdir == "/srv/app"
        ps_args = m.call_args_list[0].args[0]
        assert "label=com.docker.compose.project=stack" in ps_args
        # Every member inspected in one call, not one call each.
        assert m.call_args_list[1].args[0][:2] == ["docker", "inspect"]
        assert len(m.call_args_list) == 2

    def test_twelve_members_still_cost_two_calls(self):
        """The fullctl case: member count must not become a call count."""
        ps = _ps_result(
            "".join(f"id{n:02d}\tstack-svc{n:02d}-1\n" for n in range(12))
        )
        inspect = _inspect_result([
            (f"id{n:02d}", f"/stack-svc{n:02d}-1", "/app",
             f'{{"com.docker.compose.service":"svc{n:02d}"}}')
            for n in range(12)
        ])
        with patch(
            "lmer_cli.service.subprocess.run", side_effect=[ps, inspect]
        ) as m:
            members = resolve_group("docker", "stack", announce=False)

        assert len(members) == 12
        assert len(m.call_args_list) == 2

    def test_member_without_a_service_label_is_keyed_by_container_name(self):
        """A container run into the project by hand is still reachable."""
        ps = _ps_result("ccc333\tstray\n")
        inspect = _inspect_result([
            ("ccc333", "/stray", "", '{"com.docker.compose.project":"stack"}'),
        ])
        with patch("lmer_cli.service.subprocess.run", side_effect=[ps, inspect]):
            members = resolve_group("docker", "stack", announce=False)

        assert members[0].service == "stray"
        assert members[0].workdir == "/"  # empty WorkingDir falls back

    def test_unparseable_labels_do_not_drop_the_member(self):
        """One broken labels blob must not take the listing down with it."""
        ps = _ps_result("ddd444\tstack-web-1\n")
        inspect = _inspect_result([("ddd444", "/stack-web-1", "/app", "not-json")])
        with patch("lmer_cli.service.subprocess.run", side_effect=[ps, inspect]):
            members = resolve_group("docker", "stack", announce=False)

        assert [m_.service for m_ in members] == ["stack-web-1"]

    def test_empty_project_errors_and_names_the_running_projects(self):
        """A typo'd project gets the list that would have worked."""
        empty = _ps_result("")
        all_ids = _ps_result("eee555\n")
        inspect = _inspect_result([
            ("eee555", "/other-web-1", "/app",
             '{"com.docker.compose.project":"other"}'),
        ])
        with patch(
            "lmer_cli.service.subprocess.run",
            side_effect=[empty, all_ids, inspect],
        ):
            with pytest.raises(ServiceError) as exc:
                resolve_group("docker", "stcak", announce=False)

        assert "stcak" in str(exc.value)
        assert "other" in str(exc.value)

    def test_inspect_failure_with_no_usable_row_is_a_service_error(self):
        """Both id spellings tried (see _ID_FIELDS), then it is an error."""
        ps = _ps_result("aaa111\tstack-web-1\n")
        failed = _ps_result("", returncode=1, stderr="no such object")
        with patch(
            "lmer_cli.service.subprocess.run",
            side_effect=[ps, failed, failed],
        ) as m:
            with pytest.raises(ServiceError, match="no such object"):
                resolve_group("docker", "stack", announce=False)
        templates = [
            call.args[0][-1] for call in m.call_args_list[1:]
        ]
        assert templates[0].startswith("{{.ID}}")
        assert templates[1].startswith("{{.Id}}")


class TestMemberOf:
    """Tests for member_of — membership is checked against the resolved group."""

    def _members(self) -> list:
        return [
            ServiceMember("web", "aaa111", "stack-web-1", "/srv/app"),
            ServiceMember("db", "bbb222", "stack-db-1", "/"),
        ]

    def test_returns_the_named_member(self):
        assert member_of(self._members(), "web").container_id == "aaa111"

    def test_non_member_errors_with_the_member_list(self):
        with pytest.raises(ServiceError) as exc:
            member_of(self._members(), "cache")
        assert "cache" in str(exc.value)
        assert "web" in str(exc.value) and "db" in str(exc.value)


class TestScaledServices:
    """A scaled service: one declared name, several running containers."""

    def _scaled(self):
        ps = _ps_result("aaa111\tstack-web-1\nbbb222\tstack-web-2\nccc333\tstack-db-1\n")
        inspect = _inspect_result([
            ("aaa111", "/stack-web-1", "/app",
             '{"com.docker.compose.service":"web"}'),
            ("bbb222", "/stack-web-2", "/app",
             '{"com.docker.compose.service":"web"}'),
            ("ccc333", "/stack-db-1", "/",
             '{"com.docker.compose.service":"db"}'),
        ])
        return ps, inspect

    def _members(self):
        ps, inspect = self._scaled()
        with patch("lmer_cli.service.subprocess.run", side_effect=[ps, inspect]):
            return resolve_group("docker", "stack", announce=False)

    def test_the_service_name_does_not_depend_on_the_replica_count(self):
        """The name a member is addressed by must not move when a sibling stops:
        a group session records these names, and a name that changed under it
        would leave the container it holds reading free to the slot gate."""
        assert [m.service for m in self._members()] == ["db", "web", "web"]

    def test_a_scaled_service_name_is_refused_as_ambiguous(self):
        """resolve_container's answer for the same ambiguity: nobody asked for
        *that* replica."""
        with pytest.raises(ServiceError, match="2 running replicas"):
            member_of(self._members(), "web")

    def test_a_replica_is_addressed_by_its_container_name(self):
        assert member_of(self._members(), "stack-web-2").container_id == "bbb222"

    def test_an_unscaled_service_resolves_by_its_service_name(self):
        assert member_of(self._members(), "db").container_id == "ccc333"

    def test_the_listing_names_the_replicas(self):
        assert describe_members(self._members()) == [
            "db", "web (2 replicas: stack-web-1, stack-web-2)",
        ]

    def test_recorded_names_cover_both_spellings(self):
        """What a group session holds: a slot preset may name either spelling."""
        assert member_names(self._members()) == [
            "db", "web", "stack-db-1", "stack-web-1", "stack-web-2",
        ]

    def test_scaling_down_does_not_change_the_service_name(self):
        """The regression this rule exists for: with one replica left, 'web' is
        still 'web', so a member list recorded at scale=3 still matches."""
        ps = _ps_result("aaa111\tstack-web-1\n")
        inspect = _inspect_result([
            ("aaa111", "/stack-web-1", "/app",
             '{"com.docker.compose.service":"web"}'),
        ])
        with patch("lmer_cli.service.subprocess.run", side_effect=[ps, inspect]):
            members = resolve_group("docker", "stack", announce=False)

        assert [m.service for m in members] == ["web"]
        assert member_of(members, "web").container_id == "aaa111"
        assert "web" in member_names(members)


class TestPartialInspect:
    """A group is a live stack: containers come and go mid-read."""

    def test_a_vanished_container_does_not_fail_the_group(self):
        """`inspect a-gone b-present` prints the present row, complains on
        stderr and exits 1. The surviving members are the answer — the stack is
        up, and reporting it down flips a fleet row and refuses a spawn."""
        ps = _ps_result("aaa111\tstack-web-1\nbbb222\tstack-db-1\n")
        partial = _inspect_result([
            ("aaa111", "/stack-web-1", "/app",
             '{"com.docker.compose.service":"web"}'),
        ])
        partial.returncode = 1
        partial.stderr = "Error: No such object: bbb222"
        with patch("lmer_cli.service.subprocess.run", side_effect=[ps, partial]):
            members = resolve_group("docker", "stack", announce=False)

        assert [m.service for m in members] == ["web"]

    def test_a_row_with_no_id_is_dropped(self):
        """What a runtime renders for a field it does not know; such a row names
        no container to exec into, so it cannot be a member."""
        ps = _ps_result("aaa111\tstack-web-1\nbbb222\tstack-db-1\n")
        mixed = _inspect_result([
            ("<no value>", "/stack-web-1", "/app",
             '{"com.docker.compose.service":"web"}'),
            ("bbb222", "/stack-db-1", "/", '{"com.docker.compose.service":"db"}'),
        ])
        with patch("lmer_cli.service.subprocess.run", side_effect=[ps, mixed]):
            members = resolve_group("docker", "stack", announce=False)

        assert [m.service for m in members] == ["db"]


class TestIdFieldFallback:
    """Neither id spelling can take the feature down on either runtime."""

    def test_the_second_spelling_is_tried_when_the_first_yields_nothing(self):
        """The podman question this removes: docker takes both spellings, and a
        runtime that resolves template keys the other way gets the other one
        rather than a group that never resolves."""
        ps = _ps_result("aaa111\tstack-web-1\n")
        unknown_field = _ps_result(
            "", returncode=125, stderr='template: unknown field ".ID"'
        )
        good = _inspect_result([
            ("aaa111", "/stack-web-1", "/app",
             '{"com.docker.compose.service":"web"}'),
        ])
        with patch(
            "lmer_cli.service.subprocess.run",
            side_effect=[ps, unknown_field, good],
        ) as m:
            members = resolve_group("docker", "stack", announce=False)

        assert [member.service for member in members] == ["web"]
        assert m.call_args_list[2].args[0][-1].startswith("{{.Id}}")

    def test_the_first_spelling_costs_one_call_when_it_works(self):
        ps = _ps_result("aaa111\tstack-web-1\n")
        good = _inspect_result([
            ("aaa111", "/stack-web-1", "/app",
             '{"com.docker.compose.service":"web"}'),
        ])
        with patch(
            "lmer_cli.service.subprocess.run", side_effect=[ps, good]
        ) as m:
            resolve_group("docker", "stack", announce=False)

        assert len(m.call_args_list) == 2
