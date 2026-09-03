"""The G8 acceptance proof: one lmer reads both work-repo forms (issue #353).

G8 is the claim that a single lmer version serves a legacy work repo and a
schema-form plexus at the same time — detecting the form by manifest presence,
resolving the project dir by registry lookup with the `{host}/{project}`
fallback, routing `_runs/`/`_specs/` by form, reading memory at both scopes,
and in schema form writing each launch its own `_runs/<slug>/<session-id>/`
record (D22). Every clause of that is already pinned somewhere at the level it
was built — the kernel in `test_work_repo_sessions.py`, the routing helpers in
`test_work_repo_schema_paths.py`, the staging composition in
`test_work_repo_git_ops.py`, the scopes in `test_work_repo_memory_scopes.py`.
What no module did was drive a whole session's worth of SHIPPED COMMANDS
against a whole tree and look at what is on disk afterwards, which is the only
form of the claim an operator can check.

So this module drives `work_repo.cli.main()` — the entry point a container
actually runs, argv and all — never the helper underneath it. A verb reached
through its own function passes in a shape the product is never invoked in;
this build has already paid for that twice (the `--answer` reroute that was
only correct in a world container start does not produce, and the `--json`
listing that had never met a YAML-native timestamp).

Three trees, one command set (`_run_the_command_set` below):

- **A schema-form plexus with a registry.** The launch context names a
  `{host}/{project}` pair; the record lands under the registry's SHORT NAME,
  a second launch on the same target gets its own session dir, neither
  touches the other, and the `_runs/<slug>/` grouping level holds session
  directories and NOTHING else. That last one is asserted on the group's
  CONTENTS rather than inferred from a resolver's answer, because D22 is a
  statement about the tree, not about a code path.
- **An unregistered repo.** Work lands in the `{host}/{project}` host bucket
  with the schema area names, no short name is invented and no registry entry
  is written (D9) — the manifest's bytes are compared before and after.
- **A legacy tree.** The same command set writes the same paths, the same
  index entries and stages the same relative paths it did before any of this
  — spelled out literally, plus the whole-tree assertion that nothing
  `_`-named was created anywhere.

The trees come from the SHARED builder (`tests.plexus_fixtures`, used
read-only), so the shape proven here is the shape the validator's own suite
validates; a private tree would let this module and the validator agree about
a plexus neither of them describes.

Two interlocks nothing else drives end to end are proven here too: `work
memory persist`/`restore` routing by `metadata.type` across the two D17 scopes
through the verb rather than the helper, and `lmer_platform.workrepo.run_dirs`
finding the session record the work CLI has just written (the platform half of
G8, elsewhere proven only against hand-planted directories).

Every guarantee below was checked by removing the thing it guards and
confirming this module goes red: the D22 session level (`is_session_form`
forced False — 17 failures), the registry lookup (resolution forced to the
bucket — 14), the D9 no-name rule (a short name derived from the URL on a
registry miss — 6), the memory type routing (every file to the project scope
— 1), the form detection (every tree read as schema — 9, all in the legacy
class) and the area-name mapping (schema areas written at the legacy names —
21). A guarantee test that still passes with its guarantee gone is the failure
this build has hit most often, so the removals are named rather than claimed —
one of them (`test_neither_session_touches_the_other`) survived the first
sweep by comparing two empty snapshots, and says so in its own docstring.

Nothing here addresses the operational work repo: every tree is built under
`tmp_path` (the builder refuses any root under it), and every git sink these
commands reach is stubbed — the `commit_work_path` writes, and the three
read-side probes (`web_url_for` under `log`/`state set`/`artifact`,
`report_uncommitted_work_items` under `commit`, `run_dir_push_status` under a
phase change) that shell out with `cwd` inside the tree, where git walks UP
and, with a `TMPDIR` inside a checkout, would read that checkout rather than
the fixture. No probe is asserted on and all are fail-soft, so the stubs
return the answers they already gave here; what runs above them — argv
parsing, form detection, resolution, path composition, file I/O and the
staging composition — is the shipped code.
`TestTheCommandSetNeverReachesGit` holds that claim to account instead of
leaving it to this paragraph: it drives the whole command set in both forms
with `run_git_command` replaced by a recorder, which has to stay empty. The
one `git` this module still runs is a direct call of the shipped filter
(`stageable_paths`, in `test_the_commit_stages_the_bucket_it_wrote_in`),
which is that assertion's subject rather than a sink a verb reached — it is
pointed at the fixture tree, and its answer for a path no repository tracks
is the same either way.
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import yaml

from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    BUCKET_HOST,
    BUCKET_PROJECT,
    PRIMARY_REPO,
    PROJECT,
    PROJECT_MEMORY_FILE,
    REMOTES,
    USER_MEMORY_FILE,
)
from tests.plexus_fixtures import SLUG as FIXTURE_SLUG
from work_repo import cli as work_cli
from work_repo import git_ops, run_state, utils

#: Two launches against one target, in the `LMER_SESSION_ID` form, and the
#: harness id a later resume of one of them arrives with. None of the three is
#: one of the fixture tree's own recorded sessions.
SID_A = "20260826-101500-1-11111"
SID_B = "20260826-113000-1-22222"
SID_RESUMED = "20260827-090000-3-33333"

#: The target this module's sessions work on — deliberately NOT the fixture's
#: own recorded run (`FIXTURE_SLUG`), so every record asserted below was
#: created by a shipped command rather than by the builder.
TARGET = f"https://{PRIMARY_REPO}/-/issues/77"
SLUG = "develop-issue-77"

#: An unregistered repo with no bucket on disk yet: the first launch creates
#: its `{host}/{project}/` holding place (D9).
UNNAMED_HOST = "gitlab.example.com"
UNNAMED_PROJECT = "team/unnamed-thing"
UNNAMED_TARGET = f"https://{UNNAMED_HOST}/{UNNAMED_PROJECT}/-/issues/5"
UNNAMED_SLUG = "develop-issue-5"

#: A log line long enough for `work log`'s 20-character floor.
LOG_LINE = "acceptance run: the dual-read command set writing its record"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context, no announcement and no synthesized session id
    carried between tests — a verdict that depends on the environment it ran
    in is not a verdict."""
    strip_lmer_env(monkeypatch)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    run_state._reset_synthesized_session_id()


def _main(argv):
    """One `work` invocation, through the real entry point."""
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _set_context(monkeypatch, root, *, host, project, session, target,
                 task="develop"):
    """The launch context a session arrives with: the work repo, the
    `{host}/{project}` pair the launcher derived from the clone URL, the task
    and its target, and this launch's harness id."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv("LMER_REPO_HOST", host)
    monkeypatch.setenv("LMER_REPO_PROJECT", project)
    monkeypatch.setenv("LMER_TASK", task)
    monkeypatch.setenv("LMER_TASK_TARGET", target)
    monkeypatch.setenv("LMER_SESSION_ID", session)


def _launch(monkeypatch, tree, session, *, target=TARGET):
    """A launch against the tree's registered project, addressed the way a
    launcher addresses it: by host and project path, never by short name."""
    host, path = PRIMARY_REPO.split("/", 1)
    _set_context(
        monkeypatch, tree.root, host=host, project=path, session=session,
        target=target,
    )


@contextmanager
def _no_push():
    """Stub every git sink the command set reaches, recording what each verb
    staged as `(paths, message)` pairs.

    The WRITE sink is `commit_work_path`, and three modules call it, each
    having imported it by name (`cli` for the per-verb durability pushes,
    `git_ops` inside `commit_work_changes`, `memory` for the persist push), so
    all three are patched — a single one would leave a real `git` running
    against a tmp tree and, worse, would silently miss the paths one of the
    verbs staged.

    Three READ sinks are stubbed for a different reason. `work log`, `work
    state set` and `work artifact` call `web_url_for` to decorate the path
    they print; `work commit` calls `report_uncommitted_work_items` to flag
    strays; and `work state set` calls `run_dir_push_status` at a phase
    boundary to advise on an unpushed deliverable. All three shell out with
    `cwd` inside the tree (the work-repo root, or the run dir), and git walks
    UP from there. Under `tmp_path` on a normal host that finds no repository
    and all three fail soft (no URL, no stray list, "clean and up to date") —
    but with `TMPDIR` inside a checkout they would read THAT checkout's
    `origin`, status and upstream, and the module whose argument is "driven
    through the shipped entry point, nothing simulated" would be the one
    quietly reading a real repo. Nothing here asserts a URL, a stray count or
    the advisory, so each stub returns exactly the fail-soft answer the call
    already produced: `None`, `0` and `(False, False)`.

    What is NOT stubbed is everything above the sinks: argv parsing, form
    detection, resolution, path composition, file I/O and the staging
    composition are all the shipped code.
    :class:`TestTheCommandSetNeverReachesGit` holds the set honest — it fails
    if a verb reaches `git` by a route this does not cover.
    """
    staged = []

    def record(paths, message=None):
        staged.append((list(paths) if isinstance(paths, list) else [paths],
                       message))
        return 0

    with patch("work_repo.cli.commit_work_path", side_effect=record), \
            patch("work_repo.git_ops.commit_work_path", side_effect=record), \
            patch("work_repo.memory.commit_work_path", side_effect=record), \
            patch("work_repo.cli.web_url_for", return_value=None), \
            patch("work_repo.cli.report_uncommitted_work_items", return_value=0), \
            patch("work_repo.cli.run_dir_push_status", return_value=(False, False)):
        yield staged


def _run_the_command_set(tmp_path, *, spec_name="spec.md"):
    """One session's worth of shipped commands, identical in every form.

    `session-start` (open or continue the record), `log`, `state set` (which
    takes the pre-execution freeze), `artifact` (which registers a spec and
    so touches the specs index), and `commit` (which stages). Returns the
    `(paths, message)` pairs the commands handed the git sink; the last one
    is `work commit`'s.
    """
    source = tmp_path / spec_name
    source.write_text("# spec\n\nThe acceptance run's spec artifact.\n",
                      encoding="utf-8")
    with _no_push() as staged:
        assert _main(["session-start"]) == 0
        assert _main(["log", LOG_LINE]) == 0
        assert _main(["state", "set", "--phase=implement"]) == 0
        assert _main(["artifact", spec_name, "--file", str(source)]) == 0
        assert _main(["commit", "-m", "acceptance run"]) == 0
    return staged


def _snapshot(directory):
    """Every file under *directory* as `{relative path: bytes}`, symlinks as
    their target — the evidence for "nothing touched this record"."""
    snapshot = {}
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory).as_posix()
        if path.is_symlink():
            snapshot[rel] = os.readlink(path)
        elif path.is_file():
            snapshot[rel] = path.read_bytes()
    return snapshot


def _state_of(rdir):
    return yaml.safe_load((rdir / run_state.STATE_FILE).read_text(encoding="utf-8"))


def _today():
    """The date an index entry created by this run is stamped with."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _entries(specs_dir):
    """The specs index as `{entry name: link target}` — a directory listing
    IS the index, so this is the whole of it."""
    return {
        entry.name: os.readlink(entry)
        for entry in sorted(specs_dir.iterdir())
        if entry.is_symlink()
    }


def _memory_file(name, kind):
    """A memory file whose own `metadata.type` decides its scope (D17)."""
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "a {kind} memory"\n'
        "metadata:\n"
        "  node_type: memory\n"
        f"  type: {kind}\n"
        "---\n"
        "\n"
        f"The {kind} fact.\n"
    )


@pytest.fixture
def agent_memory(tmp_path, monkeypatch):
    """Claude's in-container memory dir, holding one memory of each scope's
    type and the index that lists both."""
    directory = tmp_path / "agent-memory"
    directory.mkdir()
    (directory / "user_terse_replies.md").write_text(
        _memory_file("terse-replies", "user"), encoding="utf-8")
    (directory / "project_build_gotcha.md").write_text(
        _memory_file("build-gotcha", "project"), encoding="utf-8")
    (directory / "MEMORY.md").write_text(
        "- [Terse replies](user_terse_replies.md) — a user memory.\n"
        "- [Build gotcha](project_build_gotcha.md) — a project memory.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(directory))
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    return directory


# ---------------------------------------------------------------------------
# The stub set the three trees are driven behind
# ---------------------------------------------------------------------------


class TestTheCommandSetNeverReachesGit:
    """`_no_push` covers every git sink the command set reaches — asserted
    rather than claimed in a docstring.

    This module's argument is that the commands are the shipped ones, so the
    stub set is where "nothing simulated" and "nothing real touched" have to
    meet — and it was three sinks short: `web_url_for`,
    `report_uncommitted_work_items` and `run_dir_push_status` all ran real
    git, fail-soft and unasserted, behind the verbs. A git call from a verb
    runs with `cwd` inside the tree and git walks UP from there, so under a
    `TMPDIR` inside a checkout it reads the checkout, not the fixture. Rather
    than re-audit the call graph each time a verb grows, the whole command set
    runs here with `run_git_command` replaced at BOTH names that hold it
    (`git_ops` defines it, `cli` imported it by name), and the recorder has to
    stay empty.

    It runs against both forms because the verbs branch on form: a sink
    reached only in a legacy tree is still a sink.
    """

    @pytest.mark.parametrize("tree_fixture", ["schema_plexus", "legacy_plexus"])
    def test_no_git_command_survives_the_stubs(
        self, monkeypatch, tmp_path, request, tree_fixture
    ):
        tree = request.getfixturevalue(tree_fixture)
        calls = []

        def refuse(cmd, cwd, check=True):
            # The shape of a failed probe, so a caller that does reach git
            # takes its fail-soft branch and the assertion below is what
            # reports the leak.
            calls.append((list(cmd), str(cwd)))
            return 1, ""

        monkeypatch.setattr(git_ops, "run_git_command", refuse)
        monkeypatch.setattr(work_cli, "run_git_command", refuse)

        _launch(monkeypatch, tree, SID_A)
        _run_the_command_set(tmp_path)
        # …and the resume, because a phase CHANGE takes a route the first
        # `state set` of a fresh record does not: the unpushed-deliverable
        # advisory probes the run dir with git.
        _launch(monkeypatch, tree, SID_RESUMED)
        run_state._reset_synthesized_session_id()
        with _no_push():
            assert _main(["session-start", "--session", SID_A]) == 0
            assert _main(["state", "set", "--phase=review"]) == 0

        assert calls == [], (
            f"the command set reached git {len(calls)} time(s) behind "
            f"`_no_push`: {calls} — with TMPDIR inside a checkout that is a "
            "real repository being read by the acceptance module"
        )


# ---------------------------------------------------------------------------
# Tree 1: a schema-form plexus with a registry
# ---------------------------------------------------------------------------


class TestASchemaTreeResolvesByRegistryAndRecordsPerSession:
    """The launcher knows a `{host}/{project}` pair and nothing else. The
    registry turns it into a project dir, and each launch gets its own
    record under it (D8 + D22)."""

    @pytest.fixture
    def two_launches(self, monkeypatch, tmp_path, schema_plexus):
        """Two sessions of one run, in order, each running the whole command
        set — and a snapshot of the first session's record taken before the
        second one starts."""
        _launch(monkeypatch, schema_plexus, SID_A)
        _run_the_command_set(tmp_path)
        group = schema_plexus.project_dir / "_runs" / SLUG
        first = _snapshot(group / SID_A)

        _launch(monkeypatch, schema_plexus, SID_B)
        run_state._reset_synthesized_session_id()  # a second launch is a second process
        staged = _run_the_command_set(tmp_path, spec_name="spec-second.md")
        return schema_plexus, group, first, staged

    def test_the_record_lands_under_the_registrys_short_name(
        self, monkeypatch, tmp_path, schema_plexus
    ):
        """The launch context says `gitlab.example.com` / `group/project`;
        the record is written to `lmer/`, which only the registry can name."""
        _launch(monkeypatch, schema_plexus, SID_A)
        _run_the_command_set(tmp_path)

        record = schema_plexus.project_dir / "_runs" / SLUG / SID_A
        assert schema_plexus.project_dir == schema_plexus.root / PROJECT
        assert (record / run_state.STATE_FILE).is_file()
        assert (record / run_state.EVENTS_FILE).is_file()
        assert (record / "log.yaml").is_file()
        # And nowhere else: the positional address the launch context spells
        # is never created in a tree that has a name for this repo.
        host, path = PRIMARY_REPO.split("/", 1)
        assert not (schema_plexus.root / host / path.split("/")[0]).exists()

    def test_a_mirror_remote_resolves_to_the_same_project_dir(
        self, monkeypatch, tmp_path, schema_plexus
    ):
        """The lookup is across `repo:` AND `remotes:` (D8), so a clone from
        an additional remote is the same project — which is the difference
        between a registry lookup and a coincidence about directory names."""
        mirror_host, mirror_path = REMOTES[0].split("/", 1)
        _set_context(
            monkeypatch, schema_plexus.root, host=mirror_host,
            project=mirror_path, session=SID_A,
            target=f"https://{REMOTES[0]}/issues/77",
        )
        _run_the_command_set(tmp_path)

        assert (schema_plexus.project_dir / "_runs" / SLUG / SID_A
                / run_state.STATE_FILE).is_file()
        assert not (schema_plexus.root / mirror_host).exists()

    def test_a_second_session_on_one_target_writes_a_second_dir(
        self, two_launches
    ):
        _tree, group, _first, _staged = two_launches
        assert sorted(child.name for child in group.iterdir()) == [SID_A, SID_B]
        assert _state_of(group / SID_A)["slug"] == SLUG
        assert _state_of(group / SID_B)["slug"] == SLUG

    def test_neither_session_touches_the_other(self, two_launches):
        """The whole first record, byte for byte, across the second launch's
        entire command set — including its freeze and its commit, both of
        which address the run rather than the session.

        The snapshot's contents are asserted first, deliberately: with the
        session level gone there is no `<sid>/` directory to snapshot, two
        empty maps compare equal and this test passes having compared
        nothing.
        """
        _tree, group, first, _staged = two_launches
        assert set(first) >= {"state.yaml", "events.jsonl", "log.yaml", "spec.md"}
        assert _snapshot(group / SID_A) == first

    def test_nothing_is_written_at_the_grouping_level(self, two_launches):
        """D22's core rule, asserted on the group's CONTENTS: `_runs/<slug>/`
        holds session directories and nothing else. Inferring it from a
        resolver's answer would keep passing if the session level were
        removed, which is exactly the failure this proof exists to catch."""
        _tree, group, _first, _staged = two_launches
        children = sorted(group.iterdir())
        assert [child.name for child in children] == [SID_A, SID_B]
        assert all(child.is_dir() and not child.is_symlink() for child in children)
        assert [child.name for child in children if child.is_file()] == []
        for name in (run_state.STATE_FILE, run_state.LEGACY_STATE_FILE,
                     run_state.EVENTS_FILE, "log.yaml"):
            assert not (group / name).exists()

    def test_the_lineage_records_the_launch_that_opened_each_record(
        self, two_launches
    ):
        _tree, group, _first, _staged = two_launches
        assert _state_of(group / SID_A)[run_state.SESSIONS_FIELD] == [SID_A]
        assert _state_of(group / SID_B)[run_state.SESSIONS_FIELD] == [SID_B]

    def test_the_fleet_view_lists_both_launches(self, two_launches, capsys):
        """`work sessions` is the operator's answer to "what is running", and
        in this form the answer is per launch."""
        _tree, group, _first, _staged = two_launches
        assert _main(["sessions", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        rows = [row for row in payload["sessions"] if row["slug"] == SLUG]
        assert payload["form"] == "schema"
        assert [row["id"] for row in rows] == [SID_A, SID_B]
        assert [row["address"] for row in rows] == [
            f"{SLUG}/{SID_A}", f"{SLUG}/{SID_B}",
        ]
        assert [row["path"] for row in rows] == [
            str(group / SID_A), str(group / SID_B),
        ]

    def test_a_resume_by_session_id_continues_that_record(
        self, monkeypatch, two_launches
    ):
        """A relaunch is a new container with a new harness id: one
        `--session` handshake puts it back in the record it names, and the
        run gains no third directory."""
        tree, group, _first, _staged = two_launches
        _launch(monkeypatch, tree, SID_RESUMED)
        run_state._reset_synthesized_session_id()
        with _no_push():
            assert _main(["session-start", "--session", SID_A]) == 0
            assert _main(["state", "set", "--phase=review"]) == 0

        assert _state_of(group / SID_A)["phase"] == "review"
        assert _state_of(group / SID_B)["phase"] == "implement"
        assert _state_of(group / SID_A)[run_state.SESSIONS_FIELD] == [
            SID_A, SID_RESUMED,
        ]
        assert sorted(child.name for child in group.iterdir()) == [SID_A, SID_B]

    def test_the_specs_entry_reaches_into_the_session_dir(self, two_launches):
        """The index is a flat directory of relative symlinks; in this form
        the target has to carry the group AND the session, one level deeper
        than a legacy tree's."""
        tree, _group, _first, _staged = two_launches
        entries = _entries(tree.project_dir / "_specs")
        # And the entry NAME carries the session too: one entry per record,
        # so a second launch registering the same basename cannot unlink the
        # first launch's entry (issue #353 must-fix D).
        assert entries[f"{_today()}-{SLUG}-{SID_A}--spec.md"] == (
            f"../_runs/{SLUG}/{SID_A}/spec.md"
        )
        assert entries[f"{_today()}-{SLUG}-{SID_B}--spec-second.md"] == (
            f"../_runs/{SLUG}/{SID_B}/spec-second.md"
        )
        # Every link resolves to the file it names, from where it sits.
        for name, target in entries.items():
            assert (tree.project_dir / "_specs" / name).resolve().is_file(), target

    def test_the_commit_stages_the_paths_this_form_actually_writes(
        self, two_launches
    ):
        """The staging list follows the form: the session dir, the group (the
        address a rename vacates), the `_specs/` index and both memory scopes
        — and every one of them exists on disk, which the pre-#353 list's
        `{task}/{target}` entry never did in a schema tree."""
        tree, _group, _first, staged = two_launches
        paths, message = staged[-1]
        assert message == "acceptance run"
        assert paths == [
            f"{PROJECT}/_runs/{SLUG}/{SID_B}",
            f"{PROJECT}/_runs/{SLUG}",
            f"{PROJECT}/_specs",
            f"{PROJECT}/_agents/memory",
            "_agents/memory",
        ]
        assert [path for path in paths if not (tree.root / path).exists()] == []

    def test_the_fixtures_own_legacy_shaped_areas_are_never_created(
        self, two_launches
    ):
        """A schema tree is written at the schema names only: a `runs/` or
        `specs/` dir beside the `_`-named ones is the split tree the
        validator flags ERROR."""
        tree, _group, _first, _staged = two_launches
        assert not (tree.project_dir / "runs").exists()
        assert not (tree.project_dir / "specs").exists()
        assert not (tree.project_dir / "memory").exists()


class TestTheMasterplanBundleIsIndexedAtItsCanonicalFile:
    """The bundle lives flat under the session dir (D12) and the specs entry
    points at the BUNDLE file — never at the record-root convenience symlink
    the sync creates beside it, which would make the index a link to a link."""

    def test_the_entry_targets_the_bundle_file_in_the_session_dir(
        self, monkeypatch, tmp_path, schema_plexus
    ):
        _launch(monkeypatch, schema_plexus, SID_A)
        with _no_push():
            assert _main(["session-start"]) == 0
            record = schema_plexus.project_dir / "_runs" / SLUG / SID_A
            bundle = record / "masterplan"
            bundle.mkdir()
            (bundle / "spec.md").write_text("# bundle spec\n", encoding="utf-8")
            assert _main(["artifact", "--sync"]) == 0

        assert os.readlink(record / "spec.md") == "masterplan/spec.md"
        entries = _entries(schema_plexus.project_dir / "_specs")
        assert entries[f"{_today()}-{SLUG}-{SID_A}--spec.md"] == (
            f"../_runs/{SLUG}/{SID_A}/masterplan/spec.md"
        )

    def test_a_legacy_bundle_is_indexed_exactly_as_before(
        self, monkeypatch, tmp_path, legacy_plexus
    ):
        """One shared run dir per target is what made several bundles
        possible, so a legacy tree keeps the `<mp-slug>` level and the target
        that reaches through it."""
        _launch(monkeypatch, legacy_plexus, SID_A)
        with _no_push():
            assert _main(["session-start"]) == 0
            record = legacy_plexus.runs_dir / SLUG
            bundle = record / "masterplan" / "some-run"
            bundle.mkdir(parents=True)
            (bundle / "spec.md").write_text("# bundle spec\n", encoding="utf-8")
            assert _main(["artifact", "--sync"]) == 0

        assert os.readlink(record / "spec.md") == "masterplan/some-run/spec.md"
        entries = _entries(legacy_plexus.specs_dir)
        assert entries[f"{_today()}-{SLUG}--spec.md"] == (
            f"../runs/{SLUG}/masterplan/some-run/spec.md"
        )


# ---------------------------------------------------------------------------
# Tree 2: an unregistered repo
# ---------------------------------------------------------------------------


class TestAnUnregisteredRepoParksInTheHostBucket:
    """D9: a repo nobody has named goes to `{host}/{project}/` and stays
    there, visibly unnamed. Nothing derives a short name from a URL, and
    nothing writes the registry entry a human has not decided on."""

    @pytest.fixture
    def parked(self, monkeypatch, tmp_path, schema_plexus):
        """The whole command set for a repo absent from the registry, with
        the plexus root's inventory and the manifest's bytes captured first."""
        before = sorted(child.name for child in schema_plexus.root.iterdir())
        manifest = schema_plexus.manifest.read_bytes()
        _set_context(
            monkeypatch, schema_plexus.root, host=UNNAMED_HOST,
            project=UNNAMED_PROJECT, session=SID_A, target=UNNAMED_TARGET,
        )
        staged = _run_the_command_set(tmp_path)
        return schema_plexus, before, manifest, staged

    def test_the_work_lands_in_the_host_bucket(self, parked):
        tree, _before, _manifest, _staged = parked
        record = (tree.root / UNNAMED_HOST / UNNAMED_PROJECT / "_runs"
                  / UNNAMED_SLUG / SID_A)
        assert (record / run_state.STATE_FILE).is_file()
        assert _state_of(record)["target"] == UNNAMED_TARGET

    def test_the_bucket_carries_the_forms_own_area_names(self, parked):
        """A bucket is where work is parked WITHIN the schema form, not a
        legacy island inside it: its areas are `_runs/` and `_specs/`."""
        tree, _before, _manifest, _staged = parked
        bucket = tree.root / UNNAMED_HOST / UNNAMED_PROJECT
        assert (bucket / "_runs").is_dir()
        assert (bucket / "_specs").is_dir()
        assert not (bucket / "runs").exists()
        assert not (bucket / "specs").exists()

    def test_no_name_is_invented_for_it(self, parked):
        """The plexus root gains no short-name directory — the only thing
        created is the bucket's own host dir, which is an address, not a
        name."""
        tree, before, _manifest, _staged = parked
        after = sorted(child.name for child in tree.root.iterdir())
        assert after == before  # the host dir already existed in this tree
        assert not (tree.root / "unnamed-thing").exists()
        assert not (tree.root / "team").exists()

    def test_no_registry_entry_is_written(self, parked):
        """Naming a repo is a human decision (D9), so the manifest is not
        touched — byte for byte."""
        tree, _before, manifest, _staged = parked
        assert tree.manifest.read_bytes() == manifest

    def test_the_bucket_is_still_the_D22_session_form(self, parked):
        tree, _before, _manifest, _staged = parked
        group = tree.root / UNNAMED_HOST / UNNAMED_PROJECT / "_runs" / UNNAMED_SLUG
        assert [child.name for child in group.iterdir()] == [SID_A]
        assert not (group / run_state.STATE_FILE).exists()

    def test_the_commit_stages_the_bucket_it_wrote_in(self, parked):
        """Every path follows the work into the bucket — the same list a
        named project gets, rooted at `{host}/{project}` instead.

        The bucket's own memory scope is the one path here that does not
        exist: this session persisted no memory, and D9's holding place is
        brand new, so nothing has created it. That is the staging list naming
        where the scope WOULD be rather than a path composed wrong — the
        stager drops paths that are absent (`stageable_paths`, pinned in
        `TestCommitWorkPath`), so an unwritten scope stages nothing.
        """
        tree, _before, _manifest, staged = parked
        bucket = f"{UNNAMED_HOST}/{UNNAMED_PROJECT}"
        paths, _message = staged[-1]
        assert paths == [
            f"{bucket}/_runs/{UNNAMED_SLUG}/{SID_A}",
            f"{bucket}/_runs/{UNNAMED_SLUG}",
            f"{bucket}/_specs",
            f"{bucket}/_agents/memory",
            "_agents/memory",
        ]
        assert [path for path in paths if not (tree.root / path).exists()] == [
            f"{bucket}/_agents/memory"
        ]
        assert git_ops.stageable_paths(tree.root, paths) == [
            path for path in paths if (tree.root / path).exists()
        ]

    def test_the_fleet_view_quotes_the_bucket_address(self, parked, capsys):
        tree, _before, _manifest, _staged = parked
        assert _main(["sessions", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        (row,) = [r for r in payload["sessions"] if r["slug"] == UNNAMED_SLUG]
        assert payload["form"] == "schema"
        assert row["path"] == str(
            tree.root / UNNAMED_HOST / UNNAMED_PROJECT / "_runs"
            / UNNAMED_SLUG / SID_A
        )

    def test_a_bucket_that_already_exists_is_used_as_it_stands(
        self, monkeypatch, tmp_path, schema_plexus
    ):
        """The fixture's own unnamed repo (D9's documented shape, bucket dir
        already on disk) is joined rather than re-derived."""
        _set_context(
            monkeypatch, schema_plexus.root, host=BUCKET_HOST,
            project=BUCKET_PROJECT, session=SID_A,
            target=f"https://{BUCKET_HOST}/{BUCKET_PROJECT}/-/issues/5",
        )
        _run_the_command_set(tmp_path)

        assert (schema_plexus.bucket_dir / "_runs" / UNNAMED_SLUG / SID_A
                / run_state.STATE_FILE).is_file()


# ---------------------------------------------------------------------------
# Tree 3: a legacy tree, unchanged
# ---------------------------------------------------------------------------


class TestALegacyTreeIsUnchanged:
    """A tree with no manifest keeps its bare names, its one shared run dir
    per target and its staging list. Spelled out literally rather than
    derived from the shipped helpers: an oracle built out of the code under
    test agrees with it however it changes."""

    @pytest.fixture
    def worked(self, monkeypatch, tmp_path, legacy_plexus):
        _launch(monkeypatch, legacy_plexus, SID_A)
        staged = _run_the_command_set(tmp_path)
        return legacy_plexus, staged

    def test_the_record_is_the_run_dir_itself(self, worked):
        """No session level: `runs/<slug>/` holds the state file, as it has
        since run state existed."""
        tree, _staged = worked
        record = tree.runs_dir / SLUG
        assert (record / run_state.STATE_FILE).is_file()
        assert (record / run_state.EVENTS_FILE).is_file()
        assert (record / "log.yaml").is_file()
        assert (record / "spec.md").is_file()
        assert record.parent == tree.project_dir / "runs"

    def test_a_second_launch_shares_that_one_dir(
        self, monkeypatch, tmp_path, worked
    ):
        """The legacy record is per target, not per launch — the property
        D22 changes in the other form and must not change here."""
        tree, _staged = worked
        _launch(monkeypatch, tree, SID_B)
        run_state._reset_synthesized_session_id()
        _run_the_command_set(tmp_path, spec_name="spec-second.md")

        assert sorted(child.name for child in tree.runs_dir.iterdir()) == sorted(
            [FIXTURE_SLUG, SLUG]
        )
        record = tree.runs_dir / SLUG
        assert (record / "spec.md").is_file()
        assert (record / "spec-second.md").is_file()
        # And no lineage field: a shared dir cannot claim to be one session's.
        assert run_state.SESSIONS_FIELD not in _state_of(record)

    def test_the_specs_entry_points_where_it_always_did(self, worked):
        tree, _staged = worked
        entries = _entries(tree.specs_dir)
        assert entries[f"{_today()}-{SLUG}--spec.md"] == f"../runs/{SLUG}/spec.md"
        assert tree.specs_dir == tree.project_dir / "specs"

    def test_the_commit_stages_the_three_paths_it_always_staged(self, worked):
        """The pre-#353 list, verbatim: the task-target dir (where log and
        report output predating the run-dir unification may still sit), the
        run dir, and the specs index. No memory scope — memory in this form
        is `work memory persist`'s own push."""
        tree, staged = worked
        host, path = PRIMARY_REPO.split("/", 1)
        paths, message = staged[-1]
        assert message == "acceptance run"
        assert paths == [
            f"{host}/{path}/develop/issue-77",
            f"{host}/{path}/runs/{SLUG}",
            f"{host}/{path}/specs",
        ]
        assert (tree.root / paths[1]).is_dir()
        assert (tree.root / paths[2]).is_dir()

    def test_nothing_underscore_named_is_created_anywhere(self, worked):
        """The whole-tree statement of "unchanged": a legacy tree that grew a
        `_runs/`, an `_specs/` or an `_agents/` is the split tree the
        validator flags — and the writer that produced it would have been
        reading the form from something other than the manifest."""
        tree, _staged = worked
        assert sorted(str(p.relative_to(tree.root))
                      for p in tree.root.rglob("_*")) == []

    def test_the_fleet_view_lists_the_run_without_a_session_id(
        self, worked, capsys
    ):
        tree, _staged = worked
        assert _main(["sessions", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        (row,) = [r for r in payload["sessions"] if r["slug"] == SLUG]
        assert payload["form"] == "legacy"
        assert row["id"] is None
        assert row["dir_name"] == SLUG
        assert row["address"] == SLUG


# ---------------------------------------------------------------------------
# The two D17 memory scopes, through the verb
# ---------------------------------------------------------------------------


class TestMemoryRoutesByTypeThroughTheVerb:
    """`work memory persist` files each memory where its own `metadata.type`
    says it belongs, and `work memory restore` reads the union back. Pinned
    on the verb rather than on `memory.persist_memory`, because the verb is
    what a session's Stop hook runs."""

    def test_persist_files_each_type_in_its_own_scope(
        self, monkeypatch, schema_plexus, agent_memory
    ):
        _launch(monkeypatch, schema_plexus, SID_A)
        with _no_push() as staged:
            assert _main(["memory", "persist"]) == 0

        instance = schema_plexus.root / "_agents" / "memory"
        project = schema_plexus.project_dir / "_agents" / "memory"
        assert (instance / "user_terse_replies.md").is_file()
        assert (project / "project_build_gotcha.md").is_file()
        # Neither scope holds the other's file — the split is the point.
        assert not (instance / "project_build_gotcha.md").exists()
        assert not (project / "user_terse_replies.md").exists()
        # Each scope keeps its own index, and both are staged for commit.
        assert "user_terse_replies.md" in (instance / "MEMORY.md").read_text()
        assert "project_build_gotcha.md" in (project / "MEMORY.md").read_text()
        (paths, _message), = staged
        assert paths == [f"{PROJECT}/_agents/memory", "_agents/memory"]

    def test_restore_reads_both_scopes_of_the_tree_as_built(
        self, monkeypatch, tmp_path, schema_plexus
    ):
        """The fixture tree holds one memory in each scope already, so this
        reads what a real plexus has: the agent's dir comes back with both,
        under one merged index."""
        _launch(monkeypatch, schema_plexus, SID_A)
        agent = tmp_path / "restored-memory"
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(agent))
        monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")

        assert _main(["memory", "restore"]) == 0

        assert sorted(path.name for path in agent.iterdir()) == sorted(
            ["MEMORY.md", PROJECT_MEMORY_FILE, USER_MEMORY_FILE]
        )
        index = (agent / "MEMORY.md").read_text(encoding="utf-8")
        assert f"({USER_MEMORY_FILE})" in index
        assert f"({PROJECT_MEMORY_FILE})" in index

    def test_a_legacy_tree_keeps_its_one_scope(
        self, monkeypatch, legacy_plexus, agent_memory
    ):
        """No manifest, no second scope: every type lands in the project's
        `memory/` dir and one path is staged, exactly as before D17."""
        _launch(monkeypatch, legacy_plexus, SID_A)
        with _no_push() as staged:
            assert _main(["memory", "persist"]) == 0

        scope = legacy_plexus.project_dir / "memory"
        assert (scope / "user_terse_replies.md").is_file()
        assert (scope / "project_build_gotcha.md").is_file()
        assert not (legacy_plexus.root / "_agents").exists()
        host, path = PRIMARY_REPO.split("/", 1)
        (paths, _message), = staged
        assert paths == [f"{host}/{path}/memory"]


# ---------------------------------------------------------------------------
# The platform half of G8
# ---------------------------------------------------------------------------


class TestThePlatformWalkFindsWhatTheWorkCliWrote:
    """G8's last clause — "the platform's run walk accepts
    `_runs/<slug>/<session-id>`" — against a record the work CLI actually
    wrote, rather than one the test planted.

    `tests/test_platform_workrepo.py` pins the walk thoroughly on planted
    trees; what it cannot show is that the two halves agree about the shape.
    If the writer and the walker ever disagreed by one directory level the
    fleet view would go quietly empty, which is a failure nobody sees until
    they look for a run that is running.
    """

    def _config(self, monkeypatch, tmp_path, tree):
        from lmer_platform import config as cfg
        from lmer_platform import store

        monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
        return cfg.load({"work_repo_mirror": str(tree.root)})

    def test_the_walk_finds_the_session_dir_the_cli_wrote(
        self, monkeypatch, tmp_path, schema_plexus
    ):
        from lmer_platform import workrepo

        _launch(monkeypatch, schema_plexus, SID_A)
        _run_the_command_set(tmp_path)
        config = self._config(monkeypatch, tmp_path, schema_plexus)

        refs = {ref.slug: ref for ref in workrepo.run_dirs(config)}
        ref = refs[SLUG]
        assert ref.path == schema_plexus.project_dir / "_runs" / SLUG / SID_A
        assert ref.rel_path == f"{PROJECT}/_runs/{SLUG}/{SID_A}"
        # The identity is the registry's, not the path's: a short-name dir
        # says nothing about which repo it holds.
        assert (ref.host, ref.project) == tuple(PRIMARY_REPO.split("/", 1))

    def test_the_walk_finds_a_legacy_record_the_same_cli_wrote(
        self, monkeypatch, tmp_path, legacy_plexus
    ):
        from lmer_platform import workrepo

        _launch(monkeypatch, legacy_plexus, SID_A)
        _run_the_command_set(tmp_path)
        config = self._config(monkeypatch, tmp_path, legacy_plexus)

        refs = {ref.slug: ref for ref in workrepo.run_dirs(config)}
        host, path = PRIMARY_REPO.split("/", 1)
        assert refs[SLUG].path == legacy_plexus.runs_dir / SLUG
        assert refs[SLUG].rel_path == f"{host}/{path}/runs/{SLUG}"
