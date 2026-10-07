"""Choke-point regeneration of the project README index on every durability push.

`git_ops._commit_work_path_locked` is the one locked point every durability
push and every pending replay crosses, so that is where a project's README
index block is rendered from the records the commit will contain and staged
with them. These tests drive the real composers — `work commit`, `work state
set`, `work artifact`, `work session-end`, a memory-only persist — over a
schema-2 tree under real git (`publication2`) and pin what the spec promises:
the README lands in the same commit as the record and renders fresh on
origin; a push without record changes commits nothing; a memory-only push
neither regenerates nor stages the README but prints the name nudge; one
project's push leaves another's README alone; a pending union covers both
projects once; gate and hook deferrals touch no README and the replays
regenerate; a staging refusal puts the README's bytes back; a crash-left
generated README is swept by the next push while a human's markerless draft
is left alone until the project's next record push appends the block; a
leftover the refresh cannot stage beside an empty stageable set commits
nothing (never the whole shared worktree); an ignored, malformed or
marker-line README is skipped with a warning while the record still
publishes; a project directory that is an alias (outside the checkout, or into
a completed record) is skipped before anything is written, while a checkout
reached through a symlink still publishes; an untracked sibling is not rendered
until it pushes; regeneration is fail-soft; and the legacy fixture never
regenerates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lmer_cli import gate_lock
from tests import timing_budgets as timing
from tests.schema2_fixtures import (
    DEFAULT_PROJECT,
    GROUP,
    HOST,
    RecordSpec,
    build_schema2_plexus,
    git,
    make_env,
    manifest_text,
    memory_env,
    publication2,
    record_git_commands,
    row_ids,
    stamp,
    start_session,
    state_args,
    uid_for,
)
# The legacy `publication` fixture is driven on purpose by its own name: the
# spec keeps tests/test_work_repo_push_safety.py unchanged, and the one test
# below proves THAT fixture never regenerates, so it is imported from its home
# (pytest discovers the fixture through this module's namespace).
from tests.test_work_repo_push_safety import publication  # noqa: F401
from work_repo import cli, git_ops, memory, run_state, transactions
from work_repo import project_index as pi

PROJECT = DEFAULT_PROJECT
OTHER = "gadgets"
README = f"{PROJECT}/README.md"
#: A record committed by the fixture, so the first README of the project has a tracked row.
SEEDED = RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="seeded")
NUDGE = ("💡 this run has no name yet — work name <kebab-case> (or launch with lmer --name); "
         "the index lists it by its goal or first log line until then")
SKIPPED = f"⚠️  project index skipped: {README} — "
LEFTOVER_SKIPPED = "⚠️  project index leftover skipped: "


def _artifact_source(tmp_path: Path) -> Path:
    source = tmp_path / "notes.md"
    source.write_text("# notes\n", encoding="utf-8")
    return source


COMPOSERS = {
    "commit": lambda tmp_path: cli.cmd_commit("save"),
    "state-set": lambda tmp_path: cli.cmd_state(state_args(phase="spec")),
    "artifact": lambda tmp_path: cli.cmd_artifact("notes.md", str(_artifact_source(tmp_path))),
    "session-end": lambda tmp_path: cli.cmd_session_end(),
}


@pytest.fixture
def pub(tmp_path, monkeypatch):
    """A pushed schema-2 tree with one committed record and a fresh launch in it."""
    case = publication2(tmp_path, monkeypatch, projects={PROJECT: [SEEDED]})
    rdir, state = start_session(monkeypatch, case.root)
    return case, rdir, state


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    """Two registered projects, each with a committed record, and a launch in the first."""
    case = publication2(tmp_path, monkeypatch, projects={
        PROJECT: [SEEDED],
        OTHER: [RecordSpec(uid_for(2, "g"), "chat", stamp(2), stamp(2, 1), name="gadget-seed")],
    })
    rdir, state = start_session(monkeypatch, case.root, PROJECT)
    return case, rdir, state


# ---------------------------------------------------------------------------
# Every composer lands the README with the record
# ---------------------------------------------------------------------------


class TestTheReadmeTravelsWithTheRecord:
    @pytest.mark.parametrize("composer", sorted(COMPOSERS))
    def test_each_composer_lands_the_readme_in_the_same_commit(self, pub, tmp_path, composer):
        case, rdir, _ = pub

        assert COMPOSERS[composer](tmp_path) == 0

        files = case.commit_files()
        assert README in files
        assert any(name.startswith(f"{PROJECT}/_runs/chat/{rdir.name}/") for name in files), files
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert row_ids(published) == {uid_for(1)[:8], rdir.name[:8]}

    def test_a_second_push_without_record_changes_commits_nothing_new(self, pub, capsys):
        case, _, _ = pub
        assert cli.cmd_commit("first") == 0
        head = git(case.origin, "rev-parse", "main").stdout

        assert cli.cmd_commit("again") == 0

        assert git(case.origin, "rev-parse", "main").stdout == head
        assert "No new file changes to commit" in capsys.readouterr().out
        assert git(case.root, "status", "--porcelain", "--", README).stdout == ""

    def test_a_memory_only_push_neither_regenerates_nor_stages_the_readme_but_nudges(
        self, pub, tmp_path, monkeypatch, capsys
    ):
        case, _, _ = pub
        memory_env(monkeypatch, tmp_path)
        capsys.readouterr()

        assert memory.persist_memory("memory") == 0

        assert not case.tree.readme().exists()
        assert case.origin_file(README) is None
        assert README not in case.commit_files()
        assert NUDGE in capsys.readouterr().out

    def test_a_project_push_leaves_the_other_projects_readme_alone(self, two_projects, tmp_path):
        case, rdir, _ = two_projects

        assert cli.cmd_commit("widgets only") == 0

        assert case.origin_file(README) is not None
        assert case.origin_file(f"{OTHER}/README.md") is None
        assert not case.tree.readme(OTHER).exists()
        assert row_ids(case.origin_file(README)) == {uid_for(1)[:8], rdir.name[:8]}

    def test_a_pending_union_from_two_sessions_regenerates_both_projects_once(
        self, two_projects, tmp_path, monkeypatch
    ):
        case, first_rdir, _ = two_projects
        # A second session in the other project parks its push (the shape a
        # gate deferral leaves behind); the first session's commit replays it.
        other_rdir, _ = start_session(monkeypatch, case.root, OTHER)
        transactions.queue_paths(case.root, run_state.run_rel_path_candidates())
        make_env(monkeypatch, case.root, PROJECT)
        monkeypatch.setenv("LMER_SESSION_ID", first_rdir.name)
        written = []
        real_write = pi.write_project_readme

        def spy(project_dir, **kwargs):
            written.append(Path(project_dir).name)
            return real_write(project_dir, **kwargs)

        monkeypatch.setattr(git_ops.project_index, "write_project_readme", spy)

        assert cli.cmd_commit("union") == 0

        assert sorted(written) == sorted([PROJECT, OTHER])
        files = case.commit_files()
        assert README in files and f"{OTHER}/README.md" in files
        for project, rdir in ((PROJECT, first_rdir), (OTHER, other_rdir)):
            published, rendered = case.origin_render(project, tmp_path / "oracle")
            assert published == rendered
            assert rdir.name[:8] in row_ids(published)
        assert transactions.pending_paths(case.root) == ([], [])


# ---------------------------------------------------------------------------
# Deferrals
# ---------------------------------------------------------------------------


class TestDeferralsTouchNoReadme:
    def test_a_gate_deferral_queues_no_readme_and_the_replay_regenerates(
        self, pub, tmp_path, monkeypatch
    ):
        case, rdir, _ = pub
        monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-markers"))
        monkeypatch.setenv(gate_lock.GUARD_ENV, "1")
        gate = subprocess.Popen([
            sys.executable, "-c",
            "import sys; from lmer_cli.gate_lock import hold_gate_lock\n"
            "with hold_gate_lock('gate-check'):\n"
            " print('ready', flush=True)\n sys.stdin.read()\n",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert gate.stdout.readline().strip() == "ready"
            assert gate_lock.active_gate() is not None

            assert cli.cmd_commit("deferred") == 0

            assert not case.tree.readme().exists()
            _, pending = transactions.pending_paths(case.root)
            assert pending and README not in pending
            assert case.origin_file(README) is None
            assert git(case.root, "diff", "--cached", "--name-only").stdout == ""
        finally:
            gate.communicate(timeout=timing.SHUTDOWN)

        assert cli.cmd_commit("replay") == 0

        assert README in case.commit_files()
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered and rdir.name[:8] in row_ids(published)
        assert transactions.pending_paths(case.root) == ([], [])

    def test_a_hook_deferral_replay_ends_with_an_empty_porcelain(self, pub, tmp_path):
        case, rdir, _ = pub
        (rdir / "initial.md").write_text("initial work\n")
        hook = case.root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)  # the fixture's empty template dir installs no hooks dir
        hook.write_text(f'''#!{sys.executable}
from pathlib import Path
from work_repo import cli, run_state
marker = Path({str(case.root / '.git' / 'hook-wrote')!r})
if not marker.exists():
    marker.touch()
    directory = run_state.run_dir()
    (directory / "hook-note.md").write_text("saved by hook\\n")
    raise SystemExit(cli.cmd_commit("nested hook durability"))
''')
        hook.chmod(0o755)
        # A child process bounds a possible parent/hook lock deadlock.
        process = subprocess.run(
            [sys.executable, "-c",
             "from work_repo.cli import cmd_commit; raise SystemExit(cmd_commit('initial publication'))"],
            env=dict(os.environ, HOME=str(tmp_path / "home")), capture_output=True,
            text=True, timeout=timing.CLI,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        assert README in case.commit_files()

        assert cli.cmd_commit("flush hook request") == 0

        rel = rdir.relative_to(case.root).as_posix()
        assert case.origin_file(f"{rel}/hook-note.md") == "saved by hook\n"
        assert git(case.root, "status", "--porcelain").stdout == ""
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered


# ---------------------------------------------------------------------------
# Refusals, leftovers and drafts
# ---------------------------------------------------------------------------


class TestTheReadmeIsRestoredOrSwept:
    @pytest.mark.parametrize("existing", [True, False], ids=["hand-readme", "no-readme"])
    def test_a_staging_refusal_restores_the_readme_bytes_and_the_index(self, pub, monkeypatch, existing):
        case, _, _ = pub
        readme = case.tree.readme()
        if existing:
            readme.write_bytes(b"# Widgets\r\n\r\nHand notes.\r\n")
            git(case.root, "add", "--", README)
            git(case.root, "commit", "-q", "-m", "hand readme")
            git(case.root, "push", "-q")
        before = readme.read_bytes() if existing else None
        # The index as content, not as a file: `git status` (which the leftover
        # sweep runs) may refresh racily-clean stat data and rewrite the file.
        index = git(case.root, "ls-files", "--stage").stdout
        head = case.head(case.root)
        monkeypatch.setattr(git_ops, "_check_staged_conflicts", lambda repo, label: 1)

        assert cli.cmd_commit("refused") != 0

        assert (readme.read_bytes() if readme.exists() else None) == before
        assert git(case.root, "ls-files", "--stage").stdout == index
        assert git(case.root, "diff", "--cached", "--name-only").stdout == ""
        assert case.head(case.root) == head
        assert git(case.origin, "rev-parse", "main").stdout.strip() == head

    def test_a_marker_carrying_leftover_is_swept_by_the_next_memory_push_and_never_stashed(
        self, pub, tmp_path, monkeypatch
    ):
        case, rdir, state = pub
        assert cli.cmd_commit("first") == 0
        # The crash shape: session-end wrote the record and the README, then
        # the container went away before the commit.
        state["phase"] = "spec"
        run_state.write_state(rdir, state)
        changed, _ = pi.write_project_readme(case.tree.project_dir(), heading=PROJECT)
        assert changed
        leftover = case.tree.readme().read_bytes()
        memory_env(monkeypatch, tmp_path)
        clean_at_pull = []

        def readme_at_pull(cmd, cwd):
            if cmd[0] == "pull":
                clean_at_pull.append(git(case.root, "status", "--porcelain", "--", README).stdout)

        record_git_commands(monkeypatch, readme_at_pull)

        assert memory.persist_memory("memory") == 0

        files = case.commit_files()
        assert README in files and any("_agents/memory" in name for name in files)
        assert not any("_runs/" in name for name in files), "the dirty record is not the memory push's to stage"
        assert case.origin_file(README) == leftover.decode("utf-8")
        assert "phase spec" in case.origin_file(README)
        assert clean_at_pull == [""]
        assert git(case.root, "stash", "list").stdout == ""
        assert git(case.root, "status", "--porcelain", "--", README).stdout == ""

    def test_a_leftover_is_swept_when_the_checkout_hides_untracked_files(
        self, pub, tmp_path, monkeypatch
    ):
        """The same crash shape in a checkout whose config hides untracked
        files from `git status`: the memory-only push still finds the
        tool-created README and commits it rather than stashing it."""
        case, _, _ = pub
        git(case.root, "config", "status.showUntrackedFiles", "no")
        changed, _ = pi.write_project_readme(case.tree.project_dir(), heading=PROJECT)
        assert changed
        memory_env(monkeypatch, tmp_path)

        assert memory.persist_memory("memory") == 0

        files = case.commit_files()
        assert README in files and any("_agents/memory" in name for name in files)
        assert case.origin_file(README) is not None
        assert git(case.root, "stash", "list").stdout == ""

    def test_a_markerless_human_draft_is_left_alone_until_the_projects_next_record_push(
        self, pub, tmp_path, monkeypatch
    ):
        case, rdir, _ = pub
        draft = "# Widgets\n\nMy notes.\n\n\n"
        case.tree.readme().write_text(draft, encoding="utf-8")
        memory_env(monkeypatch, tmp_path)

        assert memory.persist_memory("memory") == 0

        assert case.tree.readme().read_text(encoding="utf-8") == draft
        assert case.origin_file(README) is None
        assert README not in case.commit_files()

        assert cli.cmd_commit("record") == 0

        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert published.startswith("# Widgets\n\nMy notes.\n\n" + pi.START_MARKER)
        assert rdir.name[:8] in row_ids(published)


class TestAnEmptyStageableSetBesideALeftover:
    """`persist_memory` with nothing to persist calls `commit_work_path([])`.

    A leftover README takes that call past the no-op branch; when the refresh
    cannot stage the leftover, the stageable set is still empty — and an empty
    pathspec would hand `git add -A` the whole shared worktree: a sibling's
    half-written record, a stray file at the root, this session's own
    uncommitted record. Nothing may be staged, committed or pushed.
    """

    @staticmethod
    def _strays(case) -> list[str]:
        """Files no session's push may ever stage: a sibling's untracked record
        in another slug group and a stray file at the repository root."""
        sibling = case.root / PROJECT / "_runs" / "review" / uid_for(7) / "state.yaml"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("schema: 2\nslug: review\nstatus: in-progress\n", encoding="utf-8")
        stray = case.root / "stray-notes.txt"
        stray.write_text("not mine\n", encoding="utf-8")
        return [f"{PROJECT}/_runs/review/{uid_for(7)}/state.yaml", "stray-notes.txt"]

    @staticmethod
    def _assert_nothing_published(case, head: str, strays: list[str], out: str) -> None:
        assert git(case.root, "diff", "--cached", "--name-only").stdout == ""
        assert git(case.origin, "rev-parse", "main").stdout.strip() == head
        for rel in strays:
            assert git(case.root, "status", "--porcelain", "--", rel).stdout.startswith("??"), rel
        assert case.origin_file("stray-notes.txt") is None
        assert "No existing paths to commit in work repository" in out

    def test_a_newer_format_leftover_is_left_alone(self, pub, tmp_path, monkeypatch, capsys):
        case, _, _ = pub
        newer = pi.splice_block(None, pi.render_project_block([]), heading=PROJECT).replace(
            f"(lmer index format {pi.FORMAT})", f"(lmer index format {pi.FORMAT + 1})")
        assert pi.block_format(pi.extract_block(newer)) == pi.FORMAT + 1
        case.tree.readme().write_text(newer, encoding="utf-8")
        strays = self._strays(case)
        memory_env(monkeypatch, tmp_path, kind=None)
        head = git(case.origin, "rev-parse", "main").stdout.strip()
        capsys.readouterr()

        assert memory.persist_memory() == 0

        out = capsys.readouterr().out
        self._assert_nothing_published(case, head, strays, out)
        assert case.tree.readme().read_text(encoding="utf-8") == newer
        assert (f"{LEFTOVER_SKIPPED}{README} — index block is of a newer format "
                f"({pi.FORMAT + 1} > {pi.FORMAT}); upgrade lmer") in out

    @pytest.mark.parametrize("failure", ["skipped", "raised"])
    def test_a_leftover_the_refresh_cannot_stage_commits_nothing(
        self, pub, tmp_path, monkeypatch, capsys, failure
    ):
        case, _, _ = pub
        changed, _ = pi.write_project_readme(case.tree.project_dir(), heading=PROJECT)
        assert changed
        leftover = case.tree.readme().read_bytes()
        strays = self._strays(case)
        memory_env(monkeypatch, tmp_path, kind=None)
        if failure == "skipped":
            def refuse(*args, **kwargs):
                raise pi.IndexSkipped("a reason the sweep did not screen")

            monkeypatch.setattr(git_ops.project_index, "write_project_readme", refuse)
        else:
            def explode(*args, **kwargs):
                raise RuntimeError("kernel surprise")

            monkeypatch.setattr(git_ops.project_index, "project_rels_for_paths", explode)
        head = git(case.origin, "rev-parse", "main").stdout.strip()
        capsys.readouterr()

        assert memory.persist_memory("memory") == 0

        out = capsys.readouterr().out
        self._assert_nothing_published(case, head, strays, out)
        assert case.tree.readme().read_bytes() == leftover
        assert git_ops.INDEX_SKIPPED in out


class TestScreenedReadmesStillPublishTheRecord:
    def _assert_record_published_without_readme_change(self, case, rdir, before):
        files = case.commit_files()
        assert any(name.startswith(f"{PROJECT}/_runs/chat/{rdir.name}/") for name in files), files
        assert README not in files
        assert case.origin_file(README) == before

    def test_an_ignored_readme_is_skipped_with_a_warning(self, pub, capsys):
        case, rdir, _ = pub
        (case.root / ".gitignore").write_text(f"{README}\n")
        git(case.root, "add", "--", ".gitignore")
        git(case.root, "commit", "-q", "-m", "ignore the readme")
        git(case.root, "push", "-q")

        assert cli.cmd_commit("record") == 0

        self._assert_record_published_without_readme_change(case, rdir, None)
        assert not case.tree.readme().exists()
        assert f"{SKIPPED}ignored" in capsys.readouterr().out

    def test_a_malformed_marker_readme_is_skipped_with_a_warning(self, pub, capsys):
        case, rdir, _ = pub
        broken = f"# widgets\n\n{pi.START_MARKER}\n\nno end marker\n"
        case.tree.readme().write_text(broken, encoding="utf-8")
        git(case.root, "add", "--", README)
        git(case.root, "commit", "-q", "-m", "broken readme")
        git(case.root, "push", "-q")

        assert cli.cmd_commit("record") == 0

        self._assert_record_published_without_readme_change(case, rdir, broken)
        assert case.tree.readme().read_text(encoding="utf-8") == broken
        assert f"{SKIPPED}markers malformed" in capsys.readouterr().out

    def test_a_hand_text_marker_line_is_screened_and_would_otherwise_fail_the_gate(self, pub, capsys):
        case, rdir, _ = pub
        draft = "Widgets\n=======\n\nA setext heading in a human's draft.\n"
        case.tree.readme().write_text(draft, encoding="utf-8")

        assert cli.cmd_commit("record") == 0

        self._assert_record_published_without_readme_change(case, rdir, None)
        assert case.tree.readme().read_text(encoding="utf-8") == draft
        out = capsys.readouterr().out
        assert f"{SKIPPED}hand text holds a conflict-marker line" in out
        assert "indent or lengthen the line" in out
        # The same tree staged by hand is exactly what the marker gate refuses.
        git(case.root, "add", "--", README)
        assert git_ops._check_staged_conflicts(case.root, "probe") != 0
        assert "conflict marker" in capsys.readouterr().err.lower()

    def test_an_untracked_sibling_in_another_group_is_not_rendered_until_it_pushes(
        self, pub, tmp_path, monkeypatch
    ):
        case, rdir, _ = pub
        sibling = RecordSpec(uid_for(9), "review", stamp(5), stamp(5, 1), name="sibling")
        case.tree.add(PROJECT, sibling)

        assert cli.cmd_commit("mine") == 0

        published = case.origin_file(README)
        assert "sibling" not in published and uid_for(9)[:8] not in row_ids(published)
        assert git(case.root, "status", "--porcelain", "--", f"{PROJECT}/_runs/review").stdout.startswith("??")

        make_env(monkeypatch, case.root, PROJECT, task="review")
        monkeypatch.setenv("LMER_SESSION_ID", uid_for(9))
        assert cli.cmd_commit("sibling") == 0

        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert row_ids(published) == {uid_for(1)[:8], rdir.name[:8], uid_for(9)[:8]}


# ---------------------------------------------------------------------------
# The write boundary: an aliased project directory is never written through
# ---------------------------------------------------------------------------

#: A registered short name whose directory is a symlink: the core's walk hands
#: it over by name (`is_dir()` follows the link), so the refresh must refuse it.
ALIAS = "aliased"
ALIAS_README = f"{ALIAS}/README.md"
DONE = RecordSpec(uid_for(3), "chat", stamp(3), stamp(3, 1), status="complete", name="done-run")
DONE_REL = f"{PROJECT}/{DONE.dir_rel}"


def _outside_project(tmp_path: Path) -> Path:
    """A project directory outside the checkout whose README carries a
    generated block that is stale (rendered before its one record was
    written), so a regeneration through an alias would rewrite it."""
    outside = build_schema2_plexus(tmp_path / "elsewhere", {"outside": []})
    pi.write_project_readme(outside.project_dir("outside"), heading="outside")
    outside.add("outside", RecordSpec(uid_for(4, "x"), "chat", stamp(4), stamp(4, 1), name="outside-run"))
    return outside.project_dir("outside")


def _snapshot(directory: Path) -> dict[Path, bytes]:
    return {path.relative_to(directory): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def _register_alias(case) -> None:
    """Register :data:`ALIAS` in the checkout's manifest beside the fixture's projects."""
    case.tree.manifest.write_text(
        manifest_text({**case.tree.repos, ALIAS: f"{HOST}/{GROUP}/{ALIAS}"}), encoding="utf-8")


class TestTheWriteBoundary:
    """A project directory the refresh writes into must be the real directory
    its rel names: not a symlink, resolving inside the checkout, and never
    inside a `_runs/` record. The README's own symlink check sees only the
    final file, so an alias directory is skipped with the usual warning before
    anything is written, and the push's exit code is never the refusal's."""

    def test_an_alias_outside_the_checkout_is_skipped_and_the_other_project_still_indexes(
        self, pub, tmp_path, capsys
    ):
        case, rdir, _ = pub
        outside = _outside_project(tmp_path)
        before = _snapshot(outside)
        _register_alias(case)
        (case.root / ALIAS).symlink_to(outside, target_is_directory=True)
        capsys.readouterr()

        staged, written = git_ops._refresh_project_indexes(
            case.root, [f"{PROJECT}/_runs/chat/{rdir.name}", f"{ALIAS}/_runs/chat"], [])

        assert staged == [README] and [rel for rel, _ in written] == [README]
        assert _snapshot(outside) == before
        assert sorted(path.name for path in outside.iterdir()) == ["README.md", "_runs"]
        assert (f"{git_ops.INDEX_SKIPPED}{ALIAS_README} — project directory resolves outside "
                "the work repo") in capsys.readouterr().out

    def test_an_alias_into_a_completed_record_is_skipped_and_the_record_stays_byte_identical(
        self, pub, capsys
    ):
        case, _, _ = pub
        record = case.tree.add(PROJECT, DONE)[0]
        git(case.root, "add", "--", DONE_REL)
        git(case.root, "commit", "-q", "-m", "a completed record")
        git(case.root, "push", "-q")
        before = _snapshot(record)
        _register_alias(case)
        (case.root / ALIAS).symlink_to(record.relative_to(case.root), target_is_directory=True)
        capsys.readouterr()

        assert git_ops._refresh_project_indexes(case.root, [f"{ALIAS}/_runs/chat"], []) == ([], [])

        assert not (record / "README.md").exists()
        assert _snapshot(record) == before
        assert git(case.root, "status", "--porcelain", "--", DONE_REL).stdout == ""
        assert (f"{git_ops.INDEX_SKIPPED}{ALIAS_README} — project directory lies inside a "
                "_runs/ record") in capsys.readouterr().out

    def test_a_registered_alias_never_fails_the_push(self, pub, tmp_path, capsys):
        case, rdir, _ = pub
        outside = _outside_project(tmp_path)
        before = _snapshot(outside)
        _register_alias(case)
        git(case.root, "add", "--", case.tree.manifest.name)
        git(case.root, "commit", "-q", "-m", "register the alias")
        git(case.root, "push", "-q")
        (case.root / ALIAS).symlink_to(outside, target_is_directory=True)

        assert cli.cmd_commit("record") == 0

        assert _snapshot(outside) == before
        files = case.commit_files()
        assert README in files and ALIAS_README not in files
        assert any(name.startswith(f"{PROJECT}/_runs/chat/{rdir.name}/") for name in files), files
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered

    def test_a_tracked_project_dir_replaced_by_an_alias_is_neither_swept_nor_refreshed(
        self, pub, tmp_path, capsys
    ):
        """The shape a real `git status` reports: the project's README is
        tracked, then its directory becomes a symlink. The listing names the
        README (deleted) and the file read through the link carries a
        generated block, so the sweep would otherwise select it and the
        refresh write and stage it through the alias. (A push from that
        checkout fails on its own — git cannot autostash a tracked path
        beyond a symlink — so the helpers are driven directly.)"""
        case, _, _ = pub
        outside = _outside_project(tmp_path)
        _register_alias(case)
        alias = case.root / ALIAS
        (alias / "_runs").mkdir(parents=True)
        pi.write_project_readme(alias, heading=ALIAS)
        git(case.root, "add", "--", case.tree.manifest.name, ALIAS_README)
        git(case.root, "commit", "-q", "-m", "register the alias")
        git(case.root, "push", "-q")
        shutil.rmtree(alias)
        alias.symlink_to(outside, target_is_directory=True)
        assert git(case.root, "status", "--porcelain", "--", ALIAS_README).stdout.strip() == f"D {ALIAS_README}"
        before = _snapshot(outside)
        capsys.readouterr()

        assert git_ops._dirty_index_rels(case.root) == []
        assert (f"{LEFTOVER_SKIPPED}{ALIAS_README} — project directory resolves outside the "
                "work repo") in capsys.readouterr().out

        assert git_ops._refresh_project_indexes(case.root, [], [ALIAS_README]) == ([], [])

        assert _snapshot(outside) == before
        assert (f"{git_ops.INDEX_SKIPPED}{ALIAS_README} — project directory resolves outside "
                "the work repo") in capsys.readouterr().out

    def test_a_checkout_reached_through_a_symlink_still_publishes_the_readme(
        self, tmp_path, monkeypatch
    ):
        case = publication2(tmp_path, monkeypatch, projects={PROJECT: [SEEDED]})
        link = tmp_path / "work-link"
        link.symlink_to(case.root, target_is_directory=True)
        rdir, _ = start_session(monkeypatch, link)

        assert cli.cmd_commit("through the link") == 0

        files = case.commit_files()
        assert README in files
        assert any(name.startswith(f"{PROJECT}/_runs/chat/{rdir.name}/") for name in files), files
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert row_ids(published) == {uid_for(1)[:8], rdir.name[:8]}


# ---------------------------------------------------------------------------
# The nudge, fail-soft regeneration, the legacy fixture
# ---------------------------------------------------------------------------


class TestTheNudge:
    def test_it_prints_on_every_push_while_unnamed_and_stops_after_work_name(self, pub, capsys):
        case, _, _ = pub
        assert cli.cmd_commit("one") == 0
        assert cli.cmd_commit("two") == 0
        assert capsys.readouterr().out.count(NUDGE) == 2

        assert cli.cmd_name("named-run") == 0
        assert cli.cmd_commit("three") == 0

        assert NUDGE not in capsys.readouterr().out

    def test_the_nudge_names_the_record_not_the_tree(self, tmp_path, capsys):
        rdir = tmp_path / "record"
        rdir.mkdir()
        (rdir / "state.yaml").write_text("schema: 2\nname: null\nslug: chat\n")
        git_ops._nudge_unnamed(rdir)
        assert capsys.readouterr().out.strip() == NUDGE
        (rdir / "state.yaml").write_text("schema: 2\nname: tidy\nslug: chat\n")
        git_ops._nudge_unnamed(rdir)
        git_ops._nudge_unnamed(None)
        git_ops._nudge_unnamed(tmp_path / "missing")
        (rdir / "state.yaml").write_text("state: [unclosed\n")
        git_ops._nudge_unnamed(rdir)
        assert capsys.readouterr().out == ""


class TestRegenerationIsFailSoft:
    @pytest.mark.parametrize("broken", ["write_project_readme", "tracked_record_filter", "project_rels_for_paths"])
    def test_a_raising_regenerator_leaves_the_exit_code_unchanged(self, pub, monkeypatch, capsys, broken):
        case, rdir, _ = pub

        def explode(*args, **kwargs):
            raise RuntimeError("kernel surprise")

        monkeypatch.setattr(git_ops.project_index, broken, explode)

        assert cli.cmd_commit("record") == 0

        files = case.commit_files()
        assert any(name.startswith(f"{PROJECT}/_runs/chat/{rdir.name}/") for name in files), files
        assert README not in files
        assert not case.tree.readme().exists()
        assert "⚠️  project index skipped: " in capsys.readouterr().out

    def test_a_refresh_that_raises_midway_puts_the_written_readme_back(self, two_projects, monkeypatch):
        case, _, _ = two_projects
        other_rdir, _ = start_session(monkeypatch, case.root, OTHER)
        transactions.queue_paths(case.root, run_state.run_rel_path_candidates())
        make_env(monkeypatch, case.root, PROJECT)
        monkeypatch.setenv("LMER_SESSION_ID", case.tree.record_dir(PROJECT, uid_for(1)).name)
        real_write = pi.write_project_readme
        seen = []

        def second_write_explodes(project_dir, **kwargs):
            seen.append(Path(project_dir).name)
            if len(seen) == 2:
                raise OSError("disk went away")
            return real_write(project_dir, **kwargs)

        monkeypatch.setattr(git_ops.project_index, "write_project_readme", second_write_explodes)

        assert git_ops._refresh_project_indexes(
            case.root, [f"{PROJECT}/_runs/chat", f"{OTHER}/_runs/chat"], []) == ([], [])

        assert len(seen) == 2
        assert not case.tree.readme(PROJECT).exists() and not case.tree.readme(OTHER).exists()


class TestTheLegacyFixtureNeverRegenerates:
    def test_a_legacy_publication_touches_no_readme_and_runs_no_index_git_calls(self, publication, monkeypatch):
        case = publication
        writes = []
        monkeypatch.setattr(git_ops.project_index, "write_project_readme",
                            lambda *args, **kwargs: writes.append(args) or (False, None))
        commands = record_git_commands(monkeypatch)
        case.write("new fact\n")

        assert case.publish() == 0

        assert writes == []
        assert git_ops._index_project_rels(case.repo) == []
        assert not any(cmd[:3] == ["status", "--porcelain", "-z"] or cmd[0] == "check-ignore"
                       for cmd in commands)
        assert not any(name.endswith("README.md")
                       for name in git(case.origin, "ls-tree", "-r", "--name-only", "main").stdout.split())


# ---------------------------------------------------------------------------
# The helpers behind the choke point
# ---------------------------------------------------------------------------


class TestIndexProjectRels:
    def test_registered_projects_in_registry_order(self, two_projects):
        case, _, _ = two_projects
        assert git_ops._index_project_rels(case.root) == [PROJECT, OTHER]

    def test_a_legacy_tree_has_none(self, tmp_path):
        (tmp_path / "git.example.com" / "group" / "project" / "runs").mkdir(parents=True)
        assert git_ops._index_project_rels(tmp_path) == []

    def test_a_broken_registry_falls_back_to_the_sessions_base(self, pub):
        case, _, _ = pub
        case.tree.manifest.write_text(
            "schema: 2\nname: example\nprojects:\n  widgets:\n    repo: git.example.test/acme/widgets\n"
            "  twin:\n    repo: git.example.test/acme/widgets\n", encoding="utf-8")
        assert git_ops._index_project_rels(case.root) == [f"git.example.test/acme/{PROJECT}"]


class TestDirtyIndexRels:
    def test_no_projects_means_no_git_call(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(git_ops, "run_git_command",
                            lambda cmd, cwd, check=True: calls.append(cmd) or (0, ""))
        assert git_ops._dirty_index_rels(tmp_path) == []
        assert calls == []

    def test_only_a_marker_carrying_marker_line_free_readme_counts(self, two_projects, capsys):
        case, _, _ = two_projects
        tool = pi.splice_block(None, pi.render_project_block([]), heading=PROJECT)
        case.tree.readme(PROJECT).write_text(tool, encoding="utf-8")
        case.tree.readme(OTHER).write_text("# gadgets\n\nA human draft, no markers.\n", encoding="utf-8")
        assert git_ops._dirty_index_rels(case.root) == [README]
        assert capsys.readouterr().out == ""

        case.tree.readme(OTHER).write_text("Gadgets\n=======\n\n" + pi.render_project_block([]) + "\n")
        assert git_ops._dirty_index_rels(case.root) == [README]
        assert f"{LEFTOVER_SKIPPED}{OTHER}/README.md" in capsys.readouterr().out

        case.tree.readme(OTHER).write_text(f"# gadgets\n\n{pi.START_MARKER}\n\nhalf\n", encoding="utf-8")
        assert git_ops._dirty_index_rels(case.root) == [README]

    def test_a_clean_tracked_readme_is_not_a_leftover(self, pub):
        case, _, _ = pub
        assert cli.cmd_commit("first") == 0
        assert git_ops._dirty_index_rels(case.root) == []

    def test_an_untracked_leftover_is_found_when_the_checkout_hides_untracked_files(self, pub):
        """A checkout with `status.showUntrackedFiles=no` reports no untracked
        file to a bare porcelain, and a tool-created leftover README is
        untracked until its first commit; the sweep names its own untracked
        mode on the command line, so the config cannot hide the leftover."""
        case, _, _ = pub
        git(case.root, "config", "status.showUntrackedFiles", "no")
        tool = pi.splice_block(None, pi.render_project_block([]), heading=PROJECT)
        case.tree.readme().write_text(tool, encoding="utf-8")
        assert git(case.root, "status", "--porcelain", "--", README).stdout == ""

        assert git_ops._dirty_index_rels(case.root) == [README]

    def test_a_newer_format_leftover_is_not_a_sweep_target(self, pub, capsys):
        """This runtime must not downgrade a newer runtime's block, so it never
        selects one for the sweep: it names it and leaves it alone."""
        case, _, _ = pub
        tool = pi.splice_block(None, pi.render_project_block([]), heading=PROJECT)
        case.tree.readme().write_text(
            tool.replace(f"(lmer index format {pi.FORMAT})", f"(lmer index format {pi.FORMAT + 1})"),
            encoding="utf-8")

        assert git_ops._dirty_index_rels(case.root) == []

        assert (f"{LEFTOVER_SKIPPED}{README} — index block is of a newer format "
                f"({pi.FORMAT + 1} > {pi.FORMAT}); upgrade lmer") in capsys.readouterr().out


class TestRestoreIndexFiles:
    def test_previous_bytes_come_back_and_a_new_file_goes_away(self, tmp_path):
        kept = tmp_path / "kept.md"
        kept.write_bytes(b"regenerated\n")
        fresh = tmp_path / "fresh.md"
        fresh.write_bytes(b"regenerated\n")
        git_ops._restore_index_files(tmp_path, [("kept.md", b"original\r\n"), ("fresh.md", None),
                                                ("missing.md", None)])
        assert kept.read_bytes() == b"original\r\n"
        assert not fresh.exists()


class TestTheGitBaseEnvironment:
    def test_every_git_call_pins_the_editors_and_the_locale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_EDITOR", "false")
        monkeypatch.setenv("GIT_SEQUENCE_EDITOR", "false")
        seen = {}
        real_run = subprocess.run

        def spy(cmd, **kwargs):
            seen.update(kwargs["env"])
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(git_ops.subprocess, "run", spy)
        git_ops.run_git_command(["--version"], tmp_path, check=False)
        assert seen["GIT_EDITOR"] == "true" and seen["GIT_SEQUENCE_EDITOR"] == "true"
        assert seen["LC_ALL"] == "C"
