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
import subprocess
from pathlib import Path

import pytest


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
