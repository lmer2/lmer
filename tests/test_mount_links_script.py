"""Tests for Ctl/container/setup-mount-links.sh.

The script runs as ``developer`` inside the session container, before any
harness starts, and turns the ``declared:staged`` pairs in ``LMER_MOUNT_LINKS``
into symlinks (#293/#290): a bind mount at a path whose parents the image does
not ship leaves those parents root-owned, and the container cannot chown them,
so the mount is staged and the declared path is a link the container user makes
itself.

The real script is executed here (not a re-implementation) against trees under
``tmp_path``, the same way tests/test_container_limits_script.py runs the
cgroup script.
"""
import os
import subprocess
from pathlib import Path

import pytest

from lmer_cli.mounts import MOUNT_LINK_MARKER, read_link_marker


REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "Ctl" / "container" / "setup-mount-links.sh"
ENTRYPOINT = REPO_ROOT / "Ctl" / "container" / "entrypoint.sh"


def run_linker(links, home=None):
    """Run the script with *links* as LMER_MOUNT_LINKS. Returns the result.

    ``links=None`` leaves the variable unset, which is a different input from
    the empty string and is tested as such.
    """
    env = {"PATH": "/usr/bin:/bin"}
    if links is not None:
        env["LMER_MOUNT_LINKS"] = links
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def staged(tmp_path):
    """A staged mount that exists, standing in for the runtime's bind."""
    directory = tmp_path / "staging" / "sessions" / "acme"
    directory.mkdir(parents=True)
    (directory / "wire.jsonl").write_text("{}\n", encoding="utf-8")
    return directory


class TestLinking:
    def test_the_declared_path_and_its_parents_appear(self, tmp_path, staged):
        """The point of the whole mechanism: the parent chain is created by
        *this* process, so it belongs to the user the container runs as."""
        declared = tmp_path / "home" / ".acme" / "sessions"
        result = run_linker(f"{declared}:{staged}")

        assert result.returncode == 0
        assert declared.is_symlink()
        assert declared.resolve() == staged
        assert (declared / "wire.jsonl").read_text(encoding="utf-8") == "{}\n"
        assert declared.parent.is_dir() and not declared.parent.is_symlink()
        assert result.stderr == "", result.stderr

    def test_a_second_run_changes_nothing(self, tmp_path, staged):
        """Idempotent: a session that re-runs the entrypoint (or an image whose
        harness restarts) must not lose the link it already has."""
        declared = tmp_path / "home" / ".acme" / "sessions"
        first = run_linker(f"{declared}:{staged}")
        second = run_linker(f"{declared}:{staged}")

        assert (first.returncode, second.returncode) == (0, 0)
        assert declared.is_symlink()
        assert declared.resolve() == staged
        assert second.stderr == "", second.stderr

    def test_a_symlink_pointing_elsewhere_is_repointed(self, tmp_path, staged):
        """A link this mechanism owns may be wrong (a stale target from another
        layout); repointing costs nothing and leaving it costs the mount."""
        declared = tmp_path / "home" / ".acme" / "sessions"
        declared.parent.mkdir(parents=True)
        declared.symlink_to(tmp_path / "somewhere-else")
        result = run_linker(f"{declared}:{staged}")

        assert result.returncode == 0
        assert declared.resolve() == staged

    def test_an_empty_directory_is_replaced(self, tmp_path, staged):
        """rmdir, so only an empty directory can ever give way — an image that
        ships the declared directory but nothing in it is the shape this
        handles."""
        declared = tmp_path / "home" / ".acme" / "sessions"
        declared.mkdir(parents=True)
        result = run_linker(f"{declared}:{staged}")

        assert result.returncode == 0
        assert declared.is_symlink()
        assert declared.resolve() == staged

    def test_several_pairs_are_all_linked(self, tmp_path, staged):
        creds = tmp_path / "staging" / "creds" / "0"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text("token", encoding="utf-8")
        sessions = tmp_path / "home" / ".acme" / "sessions"
        auth = tmp_path / "home" / ".local" / "share" / "acme" / "auth.json"
        result = run_linker(f"{sessions}:{staged},{auth}:{creds}")

        assert result.returncode == 0
        assert sessions.resolve() == staged
        assert auth.read_text(encoding="utf-8") == "token"


class TestSkips:
    """Every refusal is one warning line and nothing else: a link that cannot be
    made costs one harness its mount, never the session."""

    def _skipped(self, result, declared):
        assert result.returncode == 0
        assert "⚠️" in result.stderr, result.stderr
        assert not Path(declared).is_symlink()

    def test_a_non_empty_directory_is_left_alone(self, tmp_path, staged):
        declared = tmp_path / "home" / ".acme" / "sessions"
        declared.mkdir(parents=True)
        (declared / "keep-me").write_text("data", encoding="utf-8")
        result = run_linker(f"{declared}:{staged}")

        self._skipped(result, declared)
        assert (declared / "keep-me").read_text(encoding="utf-8") == "data", (
            "nothing this script cannot rmdir may be deleted"
        )

    def test_a_regular_file_is_left_alone(self, tmp_path, staged):
        declared = tmp_path / "home" / ".acme" / "auth.json"
        declared.parent.mkdir(parents=True)
        declared.write_text("real credentials", encoding="utf-8")
        result = run_linker(f"{declared}:{staged}")

        self._skipped(result, declared)
        assert declared.read_text(encoding="utf-8") == "real credentials"

    def test_a_staged_path_that_does_not_exist_is_skipped(self, tmp_path):
        """Nothing was mounted there, so a link would point at nothing — and
        the harness would create the file it should have been handed."""
        declared = tmp_path / "home" / ".acme" / "sessions"
        result = run_linker(f"{declared}:{tmp_path / 'never-mounted'}")

        self._skipped(result, declared)
        assert not declared.parent.exists(), "a skip creates nothing either"

    @pytest.mark.parametrize("pair", [
        "not-a-pair",
        "/declared:relative/staged",
        "relative/declared:/staged",
        "/declared:/staged:extra",
        ":/staged",
        "/declared:",
    ])
    def test_a_malformed_pair_is_skipped(self, pair):
        result = run_linker(pair)

        assert result.returncode == 0
        assert "⚠️" in result.stderr, result.stderr

    def test_a_second_pair_for_the_same_declared_path_loses(self, tmp_path, staged):
        """First-wins, out loud. Two producers feed the list — the platform's
        session pairs, then this launch's credential pairs — and a manifest can
        give both the same container path. Last-wins would silently repoint a
        link that was already correct, and the loser is the one that has to be
        visible."""
        other = tmp_path / "staging" / "creds" / "0"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("token", encoding="utf-8")
        declared = tmp_path / "home" / ".acme" / "sessions"
        result = run_linker(f"{declared}:{staged},{declared}:{other}")

        assert result.returncode == 0
        assert declared.resolve() == staged, "the first pair keeps the path"
        assert "already linked" in result.stderr, result.stderr
        assert other.read_text(encoding="utf-8") == "token", "the loser is untouched"

    def test_a_skipped_pair_does_not_claim_the_declared_path(self, tmp_path, staged):
        """The boundary of first-wins: a pair that linked nothing claims
        nothing, so a later pair naming the same declared path still gets its
        chance — losing a working mount to a broken earlier entry is the failure
        this mechanism exists to avoid."""
        declared = tmp_path / "home" / ".acme" / "sessions"
        result = run_linker(
            f"{declared}:{tmp_path / 'never-mounted'},{declared}:{staged}"
        )

        assert result.returncode == 0
        assert declared.resolve() == staged
        assert "does not exist" in result.stderr
        assert "already linked" not in result.stderr

    def test_a_skip_does_not_cost_the_other_pairs(self, tmp_path, staged):
        """The failure mode this guards: one bad manifest entry silently
        stopping every later harness from finding its mount."""
        blocked = tmp_path / "home" / ".blocked"
        blocked.mkdir(parents=True)
        (blocked / "occupied").write_text("x", encoding="utf-8")
        good = tmp_path / "home" / ".acme" / "sessions"
        result = run_linker(f"{blocked}:{staged},{good}:{staged}")

        assert result.returncode == 0
        assert "⚠️" in result.stderr
        assert good.resolve() == staged


class TestNothingToDo:
    @pytest.mark.parametrize("links", [None, "", ","])
    def test_no_pairs_is_a_silent_success(self, links):
        """The overwhelmingly common case — no user harness in the session —
        must not print anything into the session's first screen."""
        result = run_linker(links)

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""


class TestEntrypointInvocation:
    def test_the_entrypoint_runs_the_linker(self):
        """A linker nothing calls is a staged mount nobody can reach."""
        text = ENTRYPOINT.read_text(encoding="utf-8")
        assert "/Agents/global/Ctl/container/setup-mount-links.sh" in text, (
            "entrypoint.sh no longer invokes the mount linker"
        )

    def test_the_linker_runs_before_the_harness_environment_is_built(self):
        """Ordering is the whole contract: a runner that starts first finds no
        credentials and no session directory at the declared paths."""
        text = ENTRYPOINT.read_text(encoding="utf-8")
        assert text.index("setup-mount-links.sh") < text.index(
            "Activating container Python virtual environment"
        )

    def test_the_linker_cannot_abort_the_session(self):
        """entrypoint.sh runs under `set -e`, and a failed link must cost one
        harness its mount rather than the whole session."""
        text = ENTRYPOINT.read_text(encoding="utf-8")
        invocation = [
            line for line in text.splitlines()
            if "setup-mount-links.sh" in line and line.strip().startswith("bash")
        ]
        assert invocation, "the linker invocation vanished from entrypoint.sh"
        assert all("|| true" in line for line in invocation), invocation


class TestLinkMarker:
    """The linker records what it did where the daemon can read it (#383 item 2).

    Whether the symlink exists is a fact only the container has, and the staged
    directory is the only place both sides can see.
    """

    def test_a_link_is_recorded_in_the_staged_directory(self, tmp_path, staged):
        declared = tmp_path / "home" / ".acme" / "sessions"
        run_linker(f"{declared}:{staged}")

        record = read_link_marker(staged)
        assert record is not None
        assert record["linked"] is True
        assert record["declared"] == str(declared)
        assert record["staged"] == str(staged)
        assert record["at"].endswith("Z")

    def test_an_idempotent_re_run_still_records_the_link(self, tmp_path, staged):
        """The early-return branch is where a long-lived store spends its life."""
        declared = tmp_path / "home" / ".acme" / "sessions"
        run_linker(f"{declared}:{staged}")
        (staged / MOUNT_LINK_MARKER).unlink()
        run_linker(f"{declared}:{staged}")

        assert read_link_marker(staged)["linked"] is True

    @pytest.mark.parametrize("obstruct, detail", [
        (lambda p: (p.mkdir(parents=True), (p / "keep").write_text("x")),
         "non-empty directory"),
        (lambda p: (p.parent.mkdir(parents=True), p.write_text("x")),
         "not a directory"),
    ], ids=["a non-empty directory", "a regular file"])
    def test_a_skip_records_why_rather_than_nothing(
        self, tmp_path, staged, obstruct, detail
    ):
        """The case the fact is wanted for: mounted and empty looks exactly like
        a store nobody used."""
        declared = tmp_path / "home" / ".acme" / "sessions"
        obstruct(declared)

        run_linker(f"{declared}:{staged}")

        record = read_link_marker(staged)
        assert record["linked"] is False
        assert detail in record["detail"]

    def test_a_success_is_not_downgraded_by_a_later_pair(self, tmp_path, staged):
        """Several declared paths legitimately share one staged store, so a later
        obstructed path must not report it dead while the first link is live."""
        linked = tmp_path / "home" / ".claude" / "memory"
        obstructed = tmp_path / "home" / ".other" / "memory"
        obstructed.mkdir(parents=True)
        (obstructed / "keep").write_text("x", encoding="utf-8")

        run_linker(f"{linked}:{staged},{obstructed}:{staged}")

        assert linked.is_symlink(), "the first pair really did link"
        record = read_link_marker(staged)
        assert record["linked"] is True
        assert record["declared"] == str(linked)

    def test_a_later_success_replaces_an_earlier_failure(self, tmp_path, staged):
        """The rule is one-directional: a failure is not the last word."""
        obstructed = tmp_path / "home" / ".other" / "memory"
        obstructed.mkdir(parents=True)
        (obstructed / "keep").write_text("x", encoding="utf-8")
        linked = tmp_path / "home" / ".claude" / "memory"

        run_linker(f"{obstructed}:{staged},{linked}:{staged}")

        record = read_link_marker(staged)
        assert record["linked"] is True
        assert record["declared"] == str(linked)

    def test_a_duplicate_pair_keeps_the_link_it_already_made(self, tmp_path, staged):
        """The duplicate is refused as a *link* — it claims nothing new — but the
        store is linked, and that is what the record is about."""
        first = tmp_path / "home" / ".acme" / "sessions"
        result = run_linker(f"{first}:{staged},{first}:{staged}")

        assert "already linked — skipped" in result.stderr
        assert read_link_marker(staged)["linked"] is True

    def test_every_marker_names_the_obstruction_when_it_failed(
        self, tmp_path, staged
    ):
        """docs/PLATFORM-QUICKSTART.md promises a false `linked` names its
        obstruction, so the reader requires `detail` rather than trusting the
        writer to keep emitting it."""
        declared = tmp_path / "home" / ".acme" / "sessions"
        declared.mkdir(parents=True)
        (declared / "keep").write_text("x", encoding="utf-8")

        run_linker(f"{declared}:{staged}")

        record = read_link_marker(staged)
        assert record["linked"] is False
        assert record["detail"]

    def test_a_staged_path_that_is_a_file_gets_no_marker(self, tmp_path):
        """A staged credential is a single file — there is nothing to write into,
        and clobbering it would destroy the credential."""
        staged_file = tmp_path / "staging" / "creds" / "token"
        staged_file.parent.mkdir(parents=True)
        staged_file.write_text("secret-shaped", encoding="utf-8")
        declared = tmp_path / "home" / ".acme" / "token"

        result = run_linker(f"{declared}:{staged_file}")

        assert result.returncode == 0
        assert staged_file.read_text(encoding="utf-8") == "secret-shaped"

    @pytest.mark.skipif(
        os.geteuid() == 0,
        reason="root ignores the mode, so this cannot deny the write it is about "
               "— and GitLab CI runs as root (a standing class in this repo)",
    )
    def test_a_marker_never_costs_the_session(self, tmp_path, staged):
        """Fail-soft: an unwritable staged store loses the fact, not the link.

        Skipped as root rather than left to pass vacuously — the assertions hold
        either way there, reporting a denial path as covered when nothing was
        denied.
        """
        declared = tmp_path / "home" / ".acme" / "sessions"
        staged.chmod(0o500)
        try:
            result = run_linker(f"{declared}:{staged}")
        finally:
            staged.chmod(0o700)

        assert result.returncode == 0
        assert declared.is_symlink(), "the link still happened"

    def test_the_script_and_the_reader_spell_the_timestamp_the_same(self):
        """One format in two languages, load-bearing since `link.stale` compares
        the marker's `at` against a session's `started_at`. Parsing makes a drift
        read as "cannot tell", but it still costs the answer entirely."""
        from lmer_platform.store import TS_FORMAT

        shell = "date -u +%Y-%m-%dT%H:%M:%SZ"
        assert shell in SCRIPT.read_text(encoding="utf-8")
        assert shell.split("+", 1)[1] == TS_FORMAT, (
            "the linker's date format and lmer_platform.store.TS_FORMAT have "
            "drifted; link.stale compares values written by both"
        )

    def test_the_marker_timestamp_parses_as_a_platform_timestamp(self, tmp_path, staged):
        """The format guard above reads source; this one reads what `date` wrote."""
        from datetime import datetime
        from lmer_platform.store import TS_FORMAT

        declared = tmp_path / "home" / ".acme" / "sessions"
        run_linker(f"{declared}:{staged}")

        datetime.strptime(read_link_marker(staged)["at"], TS_FORMAT)

    def test_the_script_and_the_reader_spell_the_marker_the_same(self):
        """One name in two languages: the writer is bash and the reader is
        Python, so nothing but this test holds them together."""
        assert f'marker="{MOUNT_LINK_MARKER}"' in SCRIPT.read_text(encoding="utf-8")
