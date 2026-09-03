"""The D22 session-unit record in the run-state kernel (issue #353).

In schema form every launch owns `<project>/_runs/<slug>/<session-id>[--<name>]/`
— a complete record holding exactly the schema-1 files RUN-STATE.md documents —
and `_runs/<slug>/` is a grouping folder that holds session dirs and NOTHING
else. In a legacy tree `runs/<slug>/` is still one shared dir per target, read
and written exactly as before.

Four behaviours are load-bearing and each has its own class here:

- **Legacy is byte-identical.** The pre-session-level implementations of
  `find_run_dir`, `run_dir`, `slug_available` and the freeze gate's final name
  are copied verbatim below and used as the oracle, so "nothing changed in a
  legacy tree" is checked rather than claimed.
- **Nothing is written at the grouping level.** The whole kernel is driven
  through a launch's lifecycle against a schema tree and the group is then
  asserted to hold session directories only. The assertion is on the group's
  CONTENTS, not on a code path, so removing the session level fails it.
- **A launch picks its own session dir, a resume continues one.** The
  environment's `LMER_SESSION_ID` names this launch's dir; an explicitly named
  session is continued and the new harness id chains into its `sessions:`
  lineage, which is what routes the REST of that launch's `work` invocations
  back to the same dir with no second env var.
- **The disambiguation rule is the platform's.** When a group holds several
  session dirs the newest by session-id form wins — pinned here against
  `lmer_platform.workrepo._newest_session` itself, so the picker, the platform
  resolver and this kernel can never quote different directories of one run.
"""

import os
from pathlib import Path

import pytest
import yaml

from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    PRIMARY_REPO,
    PROJECT,
    SLUG,
    build_legacy_plexus,
    build_schema_plexus,
)
from work_repo import release_run, run_state, utils

#: Two launches against one target, in the `LMER_SESSION_ID` form.
SID_A = "20260824-101500-1-11111"
SID_B = "20260824-113000-1-22222"
#: The harness id a resume of one of them arrives with.
SID_RESUMED = "20260825-090000-3-33333"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context, and no announcement carried between tests."""
    strip_lmer_env(monkeypatch)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    run_state._reset_synthesized_session_id()


def _set_context(monkeypatch, root, *, host=None, project=None, session=None):
    """Point the resolver at *root* with the launch context a session has."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", f"https://{PRIMARY_REPO}/-/issues/12")
    for name, value in (
        ("LMER_REPO_HOST", host),
        ("LMER_REPO_PROJECT", project),
        ("LMER_SESSION_ID", session),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@pytest.fixture
def schema_tree(tmp_path):
    return build_schema_plexus(tmp_path / "plexus-schema")


@pytest.fixture
def legacy_tree(tmp_path):
    return build_legacy_plexus(tmp_path / "plexus-legacy")


def _launch(monkeypatch, tree, session=None):
    """The launch context for the fixture tree's registered project."""
    host, path = PRIMARY_REPO.split("/", 1)
    _set_context(monkeypatch, tree.root, host=host, project=path, session=session)


def _empty_schema_tree(monkeypatch, tmp_path, session=None):
    """A schema tree whose registered project has no run records yet."""
    root = tmp_path / "plexus-empty"
    root.mkdir()
    (root / "_plexus.yaml").write_text(
        f"schema: 1\nname: example\nprojects:\n  {PROJECT}:\n"
        f"    repo: {PRIMARY_REPO}\n",
        encoding="utf-8",
    )
    host, path = PRIMARY_REPO.split("/", 1)
    _set_context(monkeypatch, root, host=host, project=path, session=session)
    return root


def _rename_refused(self, target):
    """`Path.rename` on a fs that will not have it — the failure a re-slug
    has to survive without leaving anything behind."""
    raise OSError("read-only fs")


def _write_record(rdir, slug, **fields):
    """A minimal run record under *rdir* — enough for the content resolvers."""
    state = {"schema": 1, "slug": slug, "name": None, "status": "in-progress"}
    state.update(fields)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / run_state.STATE_FILE).write_text(yaml.safe_dump(state), encoding="utf-8")
    return rdir


# ---------------------------------------------------------------------------
# Legacy: byte-identical
# ---------------------------------------------------------------------------


def _reference_find_run_dir(base, identifier, match_names=False):
    """`find_run_dir()` BEFORE the session level (run_state.py:194-233 at the
    time), with the base handed in so the oracle depends on nothing this task
    touched."""
    name_match = None
    try:
        children = sorted(base.iterdir())
    except OSError:
        return None
    for child in children:
        try:
            if (
                not child.is_dir()
                or child.name.startswith(".")
                or child.name == run_state.ARCHIVE_DIR
            ):
                continue
            state = run_state._read_sibling_state(child)
        except OSError:
            continue
        if state is None:
            continue
        if state.get("slug") == identifier:
            return child
        if match_names and name_match is None and state.get("name") == identifier:
            name_match = child
    return name_match


def _reference_slug_available(base, slug, exclude=None):
    """`slug_available()` verbatim (run_state.py:238-282 at the time)."""
    recorded = _reference_find_run_dir(base, slug)
    if recorded is not None and recorded != exclude:
        return False
    try:
        bare = base / slug
        if bare.exists() and bare != exclude:
            return False
        return not any(d for d in base.glob(f"{slug}--*") if d != exclude)
    except OSError:
        return False


def _reference_run_dir(base, slug):
    """`run_dir()` verbatim (run_state.py:388-401 at the time), resolver
    fallbacks included."""
    found = _reference_find_run_dir(base, slug)
    if found is None:
        found = run_state.find_successor_run_dir(slug, base)
    if found is not None:
        return found
    corrupt = run_state._unreadable_state_candidate(base, slug)
    if corrupt is not None:
        return corrupt
    return base / slug


def _reference_freeze_name(state, rdir):
    """The freeze gate's final dir name, verbatim (run_state.py:590-620)."""
    name = state.get("name")
    slug = state.get("slug") or rdir.name
    return f"{slug}--{name}" if name else None


#: Legacy run-area shapes the resolvers have to keep answering identically:
#: the bare address, the name-bearing dir a freeze leaves, a dir whose name
#: says nothing, the dot-dir and archive skips, and a corrupt sibling.
def _populate_legacy_area(base):
    _write_record(base / "develop-issue-12", "develop-issue-12")
    _write_record(base / "develop-issue-13--named", "develop-issue-13", name="named")
    _write_record(base / "totally-unrelated", "develop-issue-14")
    _write_record(base / ".new-s-1-abc", "develop-issue-15")
    _write_record(base / run_state.ARCHIVE_DIR / "old", "develop-issue-16")
    _write_record(
        base / "release-main-v0.6.0",
        "release-main-v0.6.0",
        reslugged_from=["release-main"],
    )
    stranded = base / "develop-issue-17"
    stranded.mkdir(parents=True)
    (stranded / run_state.STATE_FILE).write_text("{[bad", encoding="utf-8")
    (base / "develop-issue-18").mkdir()  # an address occupied by an empty dir


_LEGACY_IDENTIFIERS = [
    "develop-issue-12",
    "develop-issue-13",
    "develop-issue-14",
    "develop-issue-15",
    "develop-issue-16",
    "develop-issue-17",
    "develop-issue-18",
    "develop-issue-19",
    "named",
    "release-main",
    "release-main-v0.6.0",
]


class TestLegacyIsByteIdentical:
    """A manifest-less tree resolves, seeds, freezes and re-slugs exactly as
    it did before the session level existed — checked against the verbatim
    pre-change implementations, not against a hand-written expectation."""

    @pytest.fixture
    def area(self, monkeypatch, legacy_tree):
        _launch(monkeypatch, legacy_tree, session="s-legacy-1")
        base = run_state.runs_base()
        _populate_legacy_area(base)
        return base

    @pytest.mark.parametrize("identifier", _LEGACY_IDENTIFIERS)
    def test_find_run_dir_matches_the_pre_session_implementation(
        self, area, identifier
    ):
        assert run_state.find_run_dir(identifier) == _reference_find_run_dir(
            area, identifier
        )
        assert run_state.find_run_dir(
            identifier, match_names=True
        ) == _reference_find_run_dir(area, identifier, match_names=True)

    @pytest.mark.parametrize("identifier", _LEGACY_IDENTIFIERS)
    def test_run_dir_matches_the_pre_session_implementation(self, area, identifier):
        assert run_state.run_dir(identifier) == _reference_run_dir(area, identifier)

    @pytest.mark.parametrize("identifier", _LEGACY_IDENTIFIERS)
    def test_slug_available_matches_the_pre_session_implementation(
        self, area, identifier
    ):
        assert run_state.slug_available(identifier) == _reference_slug_available(
            area, identifier
        )
        exclude = area / identifier
        assert run_state.slug_available(
            identifier, exclude=exclude
        ) == _reference_slug_available(area, identifier, exclude=exclude)

    def test_seeding_still_creates_the_bare_address(self, area):
        rdir, state = run_state.seed_run_dir("develop-issue-19", "develop", "t")
        assert rdir == area / "develop-issue-19"
        assert state["slug"] == "develop-issue-19"
        assert not list(area.glob(f"{run_state.TMP_DIR_PREFIX}s-legacy-1-*"))

    def test_the_freeze_rename_still_uses_the_slug(self, area):
        rdir, state = run_state.ensure_run("develop-issue-20")
        state["name"] = "auth-refactor"
        frozen, old_name = run_state.freeze_run_dir(rdir, state)
        assert frozen.name == _reference_freeze_name(state, rdir)
        assert frozen == area / "develop-issue-20--auth-refactor"
        assert old_name == "develop-issue-20"

    def test_a_legacy_record_gains_no_lineage_field(self, area):
        """`sessions:` is the schema form's lineage. A legacy run dir holds
        several sessions' history at once, so the chain would be a claim the
        dir cannot make — its session history stays in events.jsonl."""
        rdir, state = run_state.ensure_run("develop-issue-21")
        assert "sessions" not in run_state.load_state(rdir)
        assert run_state.attach_session(rdir, state) is False
        assert "sessions" not in run_state.load_state(rdir)

    def test_the_run_area_is_still_one_level_deep(self, area):
        """The whole point of the byte-identical rule: no session dir is
        created inside a legacy run dir."""
        rdir, _ = run_state.ensure_run("develop-issue-22")
        assert rdir.parent == area
        assert [p for p in rdir.iterdir() if p.is_dir()] == []


# ---------------------------------------------------------------------------
# Nothing is written at the grouping level (D22's core rule)
# ---------------------------------------------------------------------------


class TestNothingIsWrittenAtTheGroupingLevel:
    """`_runs/<slug>/` groups launches and holds nothing itself. Asserted on
    the directory's CONTENTS after the kernel's writers have all run, so the
    guarantee fails if the session level is removed rather than passing on a
    code path nothing reached."""

    @pytest.fixture
    def launched(self, monkeypatch, tmp_path):
        root = _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, state = run_state.ensure_run()
        return root, rdir, state

    def test_the_record_lands_one_level_below_the_group(self, launched):
        root, rdir, _ = launched
        group = root / PROJECT / "_runs" / "develop-issue-12"
        assert rdir == group / SID_A
        assert (rdir / run_state.STATE_FILE).is_file()

    def test_the_group_holds_session_dirs_and_nothing_else(self, launched):
        root, rdir, state = launched
        group = rdir.parent
        run_state.append_event(rdir, "session_start")
        run_state.write_state(rdir, state)
        run_state.set_ledger_task(rdir, "T1", "done", title="a task")
        run_state.emit_gate_event("gate-test", "pass", exit_code=0)
        state["name"] = "auth-refactor"
        rdir, _ = run_state.freeze_run_dir(rdir, state)
        assert [child.name for child in sorted(group.iterdir())] == [
            f"{SID_A}--auth-refactor"
        ]
        assert [child for child in group.iterdir() if child.is_file()] == []

    def test_a_second_launch_on_one_target_is_a_second_dir(
        self, monkeypatch, launched
    ):
        """Two people chatting about issue 12 are two records — nothing
        shared, nothing merged, never one dir (D22)."""
        root, first, _ = launched
        run_state.append_event(first, "session_start")
        monkeypatch.setenv("LMER_SESSION_ID", SID_B)
        second, second_state = run_state.ensure_run()
        assert second == first.parent / SID_B
        assert second != first
        assert second_state["slug"] == first.parent.name
        # Neither record has anything of the other's in it.
        first_events = run_state.read_events(first, last_n=0)
        assert [e["session"] for e in first_events] == [SID_A, SID_A]
        assert run_state.read_events(second, last_n=0) == [
            {**e, "session": SID_B} for e in run_state.read_events(second, last_n=0)
        ]
        assert (second / run_state.STATE_FILE).is_file()

    def test_the_group_is_never_matched_as_a_record(self, launched):
        """A grouping dir carries the slug as its NAME, which is exactly the
        string the resolver is asked for — and it still never resolves to
        one, because resolution is by recorded state (issue #87 D1)."""
        root, rdir, _ = launched
        group = rdir.parent
        assert run_state.find_run_dir("develop-issue-12") == rdir
        assert run_state.find_run_dir("develop-issue-12") != group
        assert group not in run_state.iter_record_dirs()

    def test_the_seed_lands_in_the_run_area_and_moves_once(
        self, monkeypatch, tmp_path
    ):
        """`_runs/.new-<session-id>-*` is the seed's documented home
        (SCHEMA.md, "Sessions and runs"): the run area, where the validator's
        `record.seed-dir-leftover` and the external cleaner already look —
        never the grouping level, where a leftover would be nobody's."""
        root = _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        area = run_state.runs_base()
        seen = []

        real_rename = Path.rename

        def watched(self, target):
            seen.append((self, Path(target)))
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", watched)
        rdir, _ = run_state.seed_run_dir("develop-issue-12", "develop", "t")
        moves = [pair for pair in seen if pair[0].name.startswith(run_state.TMP_DIR_PREFIX)]
        assert len(moves) == 1
        tmp, final = moves[0]
        assert tmp.parent == area
        assert tmp.name.startswith(f"{run_state.TMP_DIR_PREFIX}{SID_A}-")
        assert final == area / "develop-issue-12" / SID_A
        assert rdir == final
        assert not list(area.glob(f"{run_state.TMP_DIR_PREFIX}*"))


# ---------------------------------------------------------------------------
# Which session dir this launch is
# ---------------------------------------------------------------------------


class TestTheLaunchPicksItsOwnSessionDir:
    """`LMER_SESSION_ID` names the dir; the freeze suffix never renames the
    session; and a launch never adopts another launch's record."""

    def test_the_session_id_is_the_dir_name(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        assert run_state.run_dir().name == SID_A

    def test_a_named_session_still_answers_to_its_id(self, monkeypatch, tmp_path):
        """After the freeze the dir is `<sid>--<name>`; `<sid>` and
        `<sid>--<name>` are the same session."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, state = run_state.ensure_run()
        state["name"] = "auth-refactor"
        frozen, old_name = run_state.freeze_run_dir(rdir, state)
        run_state.write_state(frozen, state)
        assert frozen.name == f"{SID_A}--auth-refactor"
        assert run_state.run_dir() == frozen
        assert run_state.run_dir(session=SID_A) == frozen
        assert run_state.run_dir(session=frozen.name) == frozen

    def test_the_freeze_renames_the_session_not_the_group(
        self, monkeypatch, tmp_path
    ):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, state = run_state.ensure_run()
        group = rdir.parent
        state["name"] = "auth-refactor"
        frozen, old_name = run_state.freeze_run_dir(rdir, state)
        assert frozen.parent == group
        assert group.name == "develop-issue-12"
        assert old_name == f"develop-issue-12/{SID_A}"

    def test_the_freeze_reports_run_area_relative_names_to_the_index(
        self, monkeypatch, tmp_path
    ):
        """The specs index re-points relative symlinks into the record dir,
        which in schema form is two levels below the run area — so the names
        it is handed carry the group, and the staging path the caller builds
        from the returned old name addresses the dir that moved."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, state = run_state.ensure_run()
        state["name"] = "auth-refactor"
        seen = []
        monkeypatch.setattr(
            run_state.specs_index,
            "repoint_run_dir_entries",
            lambda old, new, label: seen.append((old, new, label)),
        )
        frozen, old_name = run_state.freeze_run_dir(rdir, state)
        # The label handed over is the RECORD's — the run's name plus the
        # session id — since one group holds several launches' entries.
        assert seen == [
            (
                f"develop-issue-12/{SID_A}",
                f"develop-issue-12/{SID_A}--auth-refactor",
                f"auth-refactor-{SID_A}",
            )
        ]
        assert run_state.run_rel_for_dir_name(old_name) == (
            f"{PROJECT}/_runs/develop-issue-12/{SID_A}"
        )

    def test_the_staging_candidates_carry_the_group(self, monkeypatch, tmp_path):
        """The resolved record and the bare address, as before — the bare one
        being the GROUP, which is what covers a session dir this launch has
        renamed away from (and every other session of the run)."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        run_state.ensure_run()
        assert run_state.run_rel_path_candidates() == [
            f"{PROJECT}/_runs/develop-issue-12/{SID_A}",
            f"{PROJECT}/_runs/develop-issue-12",
        ]

    def test_an_existing_record_is_never_adopted_by_a_new_launch(
        self, monkeypatch, schema_tree
    ):
        """The fixture group already holds two launches; a third arrives and
        writes its own dir rather than continuing either."""
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        rdir, _ = run_state.ensure_run()
        assert rdir == schema_tree.runs_dir / SLUG / SID_RESUMED
        for existing in schema_tree.session_dirs:
            assert existing.is_dir()
            assert rdir != existing

    def test_an_explicit_session_continues_that_record(
        self, monkeypatch, schema_tree
    ):
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        wanted = schema_tree.session_ids[0]
        assert run_state.run_dir(session=wanted) == schema_tree.session_dirs[0]
        named = schema_tree.session_ids[1]
        assert run_state.run_dir(session=named) == schema_tree.session_dirs[1]
        assert run_state.run_dir(
            session=schema_tree.session_dirs[1].name
        ) == schema_tree.session_dirs[1]

    def test_an_unknown_session_id_is_a_creation_address(
        self, monkeypatch, schema_tree
    ):
        """Same contract the slug has: an address nothing occupies resolves to
        where it WOULD be written, and the caller decides whether to seed."""
        _launch(monkeypatch, schema_tree, session=SID_A)
        assert run_state.run_dir(session=SID_RESUMED) == (
            schema_tree.runs_dir / SLUG / SID_RESUMED
        )
        assert not (schema_tree.runs_dir / SLUG / SID_RESUMED).exists()


# ---------------------------------------------------------------------------
# An explicit session id is a path segment
# ---------------------------------------------------------------------------


class TestAnExplicitSessionIdIsAPathSegment:
    """`session=` names ONE launch inside the group — it is never joined as a
    path and never expanded as a pattern.

    The rule is the one the platform already matches a resume request by:
    `lmer_platform.resume._clean_session_id` runs `answer._reject_traversal`
    over the value and `_require_session` then selects by LISTING the group and
    comparing names. Composing `group / session` instead made `.` address the
    grouping level — the one level D22 keeps empty — and globbing the group
    made `2026*` resolve to a launch the caller never named. The one
    difference from the platform is deliberate and pinned below: two matches
    resolve to the newest here rather than refusing, because this kernel sits
    under hooks that must still exit 0.
    """

    @pytest.mark.parametrize(
        "session",
        [".", "..", "../../../../etc", "develop-issue-12/" + SID_A,
         "..\\..\\etc", "", "2026*", "2026?", "[12]*"],
    )
    def test_a_traversal_or_a_pattern_is_refused(
        self, monkeypatch, schema_tree, session
    ):
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        with pytest.raises(run_state.RunStateError, match="session"):
            run_state.run_dir(session=session)

    def test_a_dot_never_writes_at_the_grouping_level(
        self, monkeypatch, schema_tree
    ):
        """`ensure_run(session=".")` used to write state.yaml and events.jsonl
        into `_runs/<slug>/` itself, which is the shape
        `record.grouping-level-entry` warns on forever after."""
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        group = schema_tree.runs_dir / SLUG
        before = sorted(p.name for p in group.iterdir())
        with pytest.raises(run_state.RunStateError):
            run_state.ensure_run(session=".")
        assert sorted(p.name for p in group.iterdir()) == before
        assert run_state.STATE_FILE not in before

    def test_a_pattern_never_resolves_to_a_launch_nobody_named(
        self, monkeypatch, schema_tree
    ):
        """`_require_session` refuses an id that names two records because "a
        whole run record is not worth guessing at"; a wildcard is that refusal
        with the guess hidden."""
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        with pytest.raises(run_state.RunStateError):
            run_state.run_dir(session="2026*")

    def test_a_refused_seed_leaves_no_orphan_behind(
        self, monkeypatch, schema_tree
    ):
        """The address is computed before the temp dir exists, so the refusal
        costs the run area no `.new-*` litter for the cleaner to sweep."""
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        with pytest.raises(run_state.RunStateError, match="session id"):
            run_state.seed_run_dir(SLUG, "develop", "issue-12", session="..")
        assert not list(schema_tree.runs_dir.glob(f"{run_state.TMP_DIR_PREFIX}*"))

    def test_an_unusable_launch_id_never_addresses_the_group_either(
        self, monkeypatch, schema_tree
    ):
        """The same door from the environment side: `LMER_SESSION_ID` decides
        a directory name too, so a value that is not one is treated as no
        launch id at all — the answer an unset variable already gets — rather
        than composed into an address at the grouping level."""
        _launch(monkeypatch, schema_tree, session=".")
        group = schema_tree.runs_dir / SLUG
        rdir, _ = run_state.ensure_run()
        assert rdir == schema_tree.session_dirs[1]  # the group's newest launch
        assert sorted(p.name for p in group.iterdir()) == [
            d.name for d in schema_tree.session_dirs
        ]

    def test_two_dirs_answering_one_id_still_resolve_to_the_newest(
        self, monkeypatch, tmp_path
    ):
        """The deliberate difference from `_require_session`, which refuses
        here — a `work` invocation runs under a hook that must exit 0, so the
        two-match case takes `_newest_record` instead."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        group = run_state.runs_base() / SLUG
        _write_record(group / f"{SID_A}--aa-first", SLUG)
        _write_record(group / f"{SID_A}--zz-second", SLUG)
        assert run_state.run_dir(session=SID_A) == group / f"{SID_A}--zz-second"


# ---------------------------------------------------------------------------
# No session id in the environment
# ---------------------------------------------------------------------------


class TestNoSessionIdInTheEnvironment:
    """A `work` invocation with no `LMER_SESSION_ID` is not a launch, so it
    never invents a launch's identity: it addresses the group's newest session
    when there is one, and only an empty group makes it start a record."""

    def test_it_addresses_the_groups_newest_session(self, monkeypatch, schema_tree):
        _launch(monkeypatch, schema_tree, session=None)
        assert run_state.run_dir() == schema_tree.session_dirs[1]

    def test_it_never_writes_a_dir_called_unknown(self, monkeypatch, tmp_path):
        root = _empty_schema_tree(monkeypatch, tmp_path, session=None)
        rdir, _ = run_state.ensure_run()
        assert rdir.name != "unknown"
        assert run_state.SESSION_ID_RE.fullmatch(rdir.name)

    def test_the_synthesized_id_is_stable_for_the_process(
        self, monkeypatch, tmp_path
    ):
        """Each `work` invocation is its own process, so a synthesized id per
        CALL would scatter one invocation's writes across several dirs."""
        _empty_schema_tree(monkeypatch, tmp_path, session=None)
        first = run_state.run_dir()
        assert run_state.run_dir() == first
        rdir, _ = run_state.ensure_run()
        assert rdir == first

    def test_the_next_invocation_joins_the_record_it_made(
        self, monkeypatch, tmp_path
    ):
        """The second contextless invocation is a new process with a new
        synthesized id — and still lands in the dir the first one wrote,
        because with no launch id the group's newest session is the address."""
        _empty_schema_tree(monkeypatch, tmp_path, session=None)
        first, _ = run_state.ensure_run()
        run_state._reset_synthesized_session_id()  # a fresh process
        second, _ = run_state.ensure_run()
        assert second == first

    def test_a_contextless_invocation_chains_no_lineage(
        self, monkeypatch, schema_tree
    ):
        """A synthesized id names this process, not a session anyone can
        resume, so it is never written into the record's lineage."""
        _launch(monkeypatch, schema_tree, session=None)
        rdir, state = run_state.ensure_run()
        assert run_state.session_lineage(run_state.load_state(rdir)) == ()

    def test_an_out_of_form_session_id_is_used_verbatim(
        self, monkeypatch, tmp_path
    ):
        """The dir name has to be the id the harness minted and every event
        records — a prettier substitute would make the record's name a
        fiction. The validator's `record.session-id-malformed` warning is
        aimed at whoever minted it."""
        _empty_schema_tree(monkeypatch, tmp_path, session="s-lc-1")
        rdir, _ = run_state.ensure_run()
        assert rdir.name == "s-lc-1"


# ---------------------------------------------------------------------------
# Resume: the lineage chain
# ---------------------------------------------------------------------------


class TestResumeChainsTheLineage:
    """A resumed session dir is a lineage across process deaths: later harness
    ids append to `sessions:` in its state.yaml, the schema int stays 1, and
    `owner` stays lineage bookkeeping — no claim is taken, because a session
    writes only its own dir."""

    def test_the_creating_launch_starts_the_chain(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, _ = run_state.ensure_run()
        state = run_state.load_state(rdir)
        assert run_state.session_lineage(state) == (SID_A,)
        assert state["schema"] == run_state.SCHEMA_VERSION == 1
        assert state["owner"] is None

    def test_a_resume_appends_the_new_harness_id(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, _ = run_state.ensure_run()
        monkeypatch.setenv("LMER_SESSION_ID", SID_RESUMED)
        resumed, state = run_state.ensure_run(session=SID_A)
        assert resumed == rdir
        assert run_state.session_lineage(state) == (SID_A, SID_RESUMED)
        assert run_state.session_lineage(run_state.load_state(rdir)) == (
            SID_A,
            SID_RESUMED,
        )
        assert state["schema"] == 1

    def test_the_chain_is_what_routes_the_rest_of_the_launch(
        self, monkeypatch, tmp_path
    ):
        """The resume handshake happens once. Every later `work` invocation is
        a new process that knows only its own `LMER_SESSION_ID` — and finds
        the same dir, because the record itself records that this harness id
        writes here. No second environment variable carries it."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, _ = run_state.ensure_run()
        monkeypatch.setenv("LMER_SESSION_ID", SID_RESUMED)
        run_state.ensure_run(session=SID_A)
        assert run_state.run_dir() == rdir
        again, _ = run_state.ensure_run()
        assert again == rdir

    def test_the_chain_is_append_only_and_deduplicated(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, state = run_state.ensure_run()
        monkeypatch.setenv("LMER_SESSION_ID", SID_RESUMED)
        run_state.ensure_run(session=SID_A)
        stamped = run_state.load_state(rdir)["updated"]
        for _ in range(3):
            run_state.ensure_run(session=SID_A)
        reread = run_state.load_state(rdir)
        assert run_state.session_lineage(reread) == (SID_A, SID_RESUMED)
        assert reread["updated"] == stamped  # nothing to say, nothing written

    def test_a_resume_of_a_named_session_finds_it_by_id(
        self, monkeypatch, tmp_path
    ):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, state = run_state.ensure_run()
        state["name"] = "auth-refactor"
        frozen, _ = run_state.freeze_run_dir(rdir, state)
        run_state.write_state(frozen, state)
        monkeypatch.setenv("LMER_SESSION_ID", SID_RESUMED)
        resumed, resumed_state = run_state.ensure_run(session=SID_A)
        assert resumed == frozen
        assert run_state.session_lineage(resumed_state) == (SID_A, SID_RESUMED)

    def test_the_lineage_reader_tolerates_anything(self):
        assert run_state.session_lineage({}) == ()
        assert run_state.session_lineage({"sessions": "s-1"}) == ()
        assert run_state.session_lineage({"sessions": [1, "s-1", None]}) == ("s-1",)
        assert run_state.session_lineage(None) == ()
        assert run_state.has_session({"sessions": ["s-1"]}, "s-1") is True
        assert run_state.has_session({"sessions": ["s-1"]}, "s-2") is False

    def test_resuming_takes_no_claim(self, monkeypatch, tmp_path):
        """D22: single writer falls out of the layout, so there is nothing to
        arbitrate — `attach_session` never touches `owner` or `claim`."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, state = run_state.ensure_run()
        state["owner"] = {"session_id": SID_A, "claimed_at": run_state.utc_now_iso()}
        run_state.write_state(rdir, state)
        monkeypatch.setenv("LMER_SESSION_ID", SID_RESUMED)
        _, resumed_state = run_state.ensure_run(session=SID_A)
        assert resumed_state["owner"] == {
            "session_id": SID_A,
            "claimed_at": state["owner"]["claimed_at"],
        }
        assert "claim" not in resumed_state


# ---------------------------------------------------------------------------
# The resolvers descend one level, and never match the group
# ---------------------------------------------------------------------------


class TestResolversDescendTheGroupingLevel:
    """`_runs/*/<sid>/state.yaml` is the scan. The grouping dir is never a
    record, and a record that is still at the legacy depth (an adopted tree
    whose runs have not been migrated) is read AND written where it lies."""

    def test_find_run_dir_resolves_through_the_group(self, monkeypatch, schema_tree):
        _launch(monkeypatch, schema_tree, session=SID_A)
        assert run_state.find_run_dir(SLUG) == schema_tree.session_dirs[1]

    def test_a_name_match_resolves_through_the_group(self, monkeypatch, schema_tree):
        _launch(monkeypatch, schema_tree, session=SID_A)
        assert run_state.find_run_dir(
            "second-look", match_names=True
        ) == schema_tree.session_dirs[1]
        assert run_state.find_run_dir("second-look") is None

    def test_the_newest_session_in_form_wins(self, monkeypatch, tmp_path):
        """The shared disambiguation rule: session ids are timestamp-first, so
        the newest by name is the newest launch."""
        root = _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        group = run_state.runs_base() / "develop-issue-12"
        _write_record(group / SID_B, "develop-issue-12")
        _write_record(group / SID_A, "develop-issue-12")
        assert run_state.find_run_dir("develop-issue-12") == group / SID_B

    def test_an_out_of_form_sibling_never_wins_the_group(
        self, monkeypatch, tmp_path
    ):
        """A hand-made `backup-before-i-broke-it/` sorts after `2026…` and
        would otherwise be quoted as the run — the defect the platform's
        `_newest_session` was fixed for, and the same rule here."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        group = run_state.runs_base() / "develop-issue-12"
        _write_record(group / SID_A, "develop-issue-12")
        _write_record(group / "zz-backup-before-i-broke-it", "develop-issue-12")
        assert run_state.find_run_dir("develop-issue-12") == group / SID_A

    def test_the_rule_is_the_platforms_own(self, monkeypatch, tmp_path):
        """Pinned against the shared implementation rather than restated: the
        picker, the platform's resolver and this kernel must never quote
        different directories of one group."""
        from lmer_platform.workrepo import _newest_session

        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        group = run_state.runs_base() / "develop-issue-12"
        _write_record(group / SID_B, "develop-issue-12")
        _write_record(group / SID_A, "develop-issue-12")
        _write_record(group / "zz-backup", "develop-issue-12")
        assert run_state.find_run_dir("develop-issue-12") == _newest_session(group)

    def test_successor_resolution_descends_too(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        base = run_state.runs_base()
        moved = _write_record(
            base / "release-main-v0.6.0" / SID_A,
            "release-main-v0.6.0",
            reslugged_from=["release-main"],
            created="2026-08-24T10:15:00Z",
        )
        assert run_state.find_successor_run_dir("release-main") == moved
        assert run_state.resolve_run_dir("release-main") == moved

    def test_slug_availability_reads_the_group_as_the_address(
        self, monkeypatch, tmp_path
    ):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        base = run_state.runs_base()
        assert run_state.slug_available("develop-issue-12")
        session = _write_record(base / "develop-issue-12" / SID_A, "develop-issue-12")
        assert not run_state.slug_available("develop-issue-12")
        assert run_state.slug_available("develop-issue-12", exclude=session)

    def test_a_record_at_the_legacy_depth_is_read_and_written_in_place(
        self, monkeypatch, tmp_path
    ):
        """An adopted tree (registry entry + `git mv`) carries its run dirs
        unmigrated: `_runs/<slug>/state.yaml`, one shared record. Reading is
        tolerant — refusing to see it would strand the run — and writing goes
        where the record already is, never into a session dir nested inside
        someone else's record."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        base = run_state.runs_base()
        unmigrated = _write_record(base / "develop-issue-12", "develop-issue-12")
        assert run_state.find_run_dir("develop-issue-12") == unmigrated
        assert run_state.run_dir() == unmigrated
        rdir, _ = run_state.ensure_run()
        assert rdir == unmigrated
        assert [p for p in unmigrated.iterdir() if p.is_dir()] == []

    def test_dot_dirs_and_the_archive_are_still_skipped(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        base = run_state.runs_base()
        _write_record(base / f"{run_state.TMP_DIR_PREFIX}{SID_A}-abc", "develop-x")
        _write_record(base / "develop-y" / f".{SID_A}", "develop-y")
        _write_record(base / run_state.ARCHIVE_DIR / "develop-z" / SID_A, "develop-z")
        assert run_state.find_run_dir("develop-x") is None
        assert run_state.find_run_dir("develop-y") is None
        assert run_state.find_run_dir("develop-z") is None

    def test_a_scan_error_still_resolves_to_no_match(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        base = run_state.runs_base()
        _write_record(base / "develop-issue-12" / SID_A, "develop-issue-12")

        def boom(self):
            raise PermissionError("run area unreadable")

        monkeypatch.setattr(Path, "iterdir", boom)
        assert run_state.find_run_dir("develop-issue-12") is None
        assert run_state.run_dir() == base / "develop-issue-12" / SID_A

    def test_a_corrupt_sibling_never_breaks_resolution(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        base = run_state.runs_base()
        broken = base / "develop-issue-12" / SID_B
        broken.mkdir(parents=True)
        (broken / run_state.STATE_FILE).write_text("{[bad", encoding="utf-8")
        good = _write_record(base / "develop-issue-12" / SID_A, "develop-issue-12")
        assert run_state.find_run_dir("develop-issue-12") == good

    def test_this_launchs_own_dir_wins_even_when_its_state_is_corrupt(
        self, monkeypatch, tmp_path
    ):
        """The session address is deterministic per launch, so a corrupt state
        file cannot strand a session the way it could strand a renamed legacy
        run dir: the dir is found by NAME and load_state recovers in place."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        base = run_state.runs_base()
        mine = base / "develop-issue-12" / SID_A
        mine.mkdir(parents=True)
        (mine / run_state.STATE_FILE).write_text("{[bad", encoding="utf-8")
        assert run_state.run_dir() == mine
        rdir, state = run_state.ensure_run()
        assert rdir == mine
        assert state["slug"] == "develop-issue-12"
        assert list(mine.glob(f"{run_state.STATE_FILE}.bad-*"))


# ---------------------------------------------------------------------------
# Re-slug: the session moves to the new identity's group
# ---------------------------------------------------------------------------


class TestReslugMovesTheSessionToItsNewGroup:
    """A release run takes a version-bearing slug mid-run. The session is the
    unit of record, so it MOVES to the group its new identity addresses,
    freeing the bare address for the next release exactly as before."""

    @pytest.fixture
    def release(self, monkeypatch, tmp_path):
        root = _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        monkeypatch.setenv("LMER_TASK", "release")
        monkeypatch.setenv("LMER_TASK_TARGET", "main")
        rdir, state = run_state.ensure_run()
        return run_state.runs_base(), rdir, state

    def test_the_session_moves_under_the_new_slug(self, release):
        base, rdir, state = release
        assert rdir == base / "release-main" / SID_A
        moved, state, old_name = run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        assert moved == base / "release-main-v0.6.0" / SID_A
        assert state["slug"] == "release-main-v0.6.0"
        assert run_state.load_state(moved)["slug"] == "release-main-v0.6.0"
        assert old_name == f"release-main/{SID_A}"

    def test_the_vacated_address_is_free_for_the_next_release(self, release):
        base, rdir, state = release
        run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        assert run_state.slug_available("release-main")
        fresh, _ = run_state.seed_run_dir("release-main", "release", "main")
        assert fresh == base / "release-main" / SID_A

    def test_the_run_is_still_reachable_from_the_address_it_left(self, release):
        base, rdir, state = release
        moved, state, _ = run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        assert run_state.resolve_run_dir("release-main") == moved
        assert run_state.run_dir("release-main") == moved

    def test_a_named_session_keeps_its_name(self, release):
        base, rdir, state = release
        state["name"] = "cut-0-6-0"
        frozen, _ = run_state.freeze_run_dir(rdir, state)
        run_state.write_state(frozen, state)
        moved, state, _ = run_state.reslug_run(frozen, state, "release-main-v0.6.0")
        assert moved == base / "release-main-v0.6.0" / f"{SID_A}--cut-0-6-0"

    def test_a_group_left_empty_is_taken_away(self, release):
        """Nothing is written at the grouping level and nothing is left there
        either: an address whose last session moved out is gone, so the next
        release can seed at it instead of finding it occupied by a husk."""
        base, rdir, state = release
        run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        assert not (base / "release-main").exists()

    def test_a_group_another_session_still_occupies_stays(
        self, monkeypatch, release
    ):
        base, rdir, state = release
        sibling = _write_record(base / "release-main" / SID_B, "release-main")
        run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        assert sibling.is_dir()
        assert (base / "release-main").is_dir()

    def test_a_failed_move_leaves_no_group_at_the_address_it_reached_for(
        self, release, monkeypatch, capsys
    ):
        """The destination group is created before the rename, so a rename
        that fails would leave an empty `_runs/<new-slug>/` nobody sweeps —
        the validator has no finding for one and no resolver is confused by
        it, but the address is occupied all the same. The move is unwound to
        exactly the tree it started from, and the failure stays fail-soft:
        loud warning, slug untouched, run carrying on where it is."""
        base, rdir, state = release

        with monkeypatch.context() as fail:
            fail.setattr(Path, "rename", _rename_refused)
            same, state, old_name = run_state.reslug_run(
                rdir, state, "release-main-v0.6.0")

        assert not (base / "release-main-v0.6.0").exists()
        assert [child.name for child in base.iterdir()] == ["release-main"]
        assert same == rdir
        assert old_name is None
        assert state["slug"] == "release-main"
        assert run_state.resolve_run_dir("release-main") == rdir
        assert "re-slug rename failed" in capsys.readouterr().err

    def test_a_failed_move_leaves_the_address_free_for_the_retry(
        self, release, monkeypatch, capsys
    ):
        """The half an operator sees. A husk at `_runs/<new-slug>/` occupies
        the address (`slug_available` counts a dir that holds no record as
        taken, deliberately), so `unique_release_slug` hands the next release
        verb a stamped variant and the run lands at
        `release-main-v0.6.0-<compact-UTC>` forever — for a reason nothing in
        the tree records. Nothing was moved, so nothing is taken."""
        base, rdir, state = release

        with monkeypatch.context() as fail:
            fail.setattr(Path, "rename", _rename_refused)
            same, state, _ = run_state.reslug_run(
                rdir, state, "release-main-v0.6.0")
        capsys.readouterr()

        assert run_state.slug_available("release-main-v0.6.0", exclude=same)
        assert release_run.unique_release_slug(
            "release-main", "0.6.0", base, same) == "release-main-v0.6.0"
        # ...and the retry actually lands there, rather than at a variant.
        moved, state, _ = run_state.reslug_run(
            same, state, "release-main-v0.6.0")
        assert moved == base / "release-main-v0.6.0" / SID_A
        assert state["slug"] == "release-main-v0.6.0"

    def test_a_failed_move_never_takes_away_a_group_holding_a_record(
        self, release, monkeypatch, capsys
    ):
        """Only a group this move CREATED and left EMPTY is unwound. The
        address is checked free before the mkdir, so the only way another
        launch's record is sitting in it by the time the rename fails is a
        race with that launch's seed — and removing the group under it would
        turn a re-slug that failed harmlessly into someone else's lost run.
        rmdir refusing a non-empty dir is the distinction, not a check that
        can drift out of step with it."""
        base, rdir, state = release
        racer = base / "release-main-v0.6.0" / SID_B

        def raced(self, target):
            _write_record(racer, "release-main-v0.6.0")
            raise OSError("read-only fs")

        with monkeypatch.context() as fail:
            fail.setattr(Path, "rename", raced)
            same, state, _ = run_state.reslug_run(
                rdir, state, "release-main-v0.6.0")

        assert racer.is_dir()
        assert run_state.load_state(racer)["slug"] == "release-main-v0.6.0"
        assert same == rdir
        assert "re-slug rename failed" in capsys.readouterr().err

    def test_a_successful_move_still_leaves_only_the_new_group(self, release):
        """The counterpart: unwinding the destination must not have cost the
        vacated group its removal. One group, holding the record, either
        way."""
        base, rdir, state = release
        moved, state, old_name = run_state.reslug_run(
            rdir, state, "release-main-v0.6.0")
        assert [child.name for child in base.iterdir()] == [
            "release-main-v0.6.0"
        ]
        assert [child.name for child in moved.parent.iterdir()] == [SID_A]
        assert old_name == f"release-main/{SID_A}"
        assert run_state.resolve_run_dir("release-main-v0.6.0") == moved


# ---------------------------------------------------------------------------
# list_sessions: what the CLI renders
# ---------------------------------------------------------------------------


class TestListSessions:
    """One row per record, in start order, with the orphaned verdict the
    `work sessions` view is built on."""

    def test_a_row_per_session_in_start_order(self, monkeypatch, schema_tree):
        _launch(monkeypatch, schema_tree, session=SID_A)
        rows = run_state.list_sessions()
        assert [row["id"] for row in rows] == list(schema_tree.session_ids)
        assert [row["path"] for row in rows] == list(schema_tree.session_dirs)
        assert [row["dir_name"] for row in rows] == [
            path.name for path in schema_tree.session_dirs
        ]

    def test_the_row_carries_the_record_the_cli_renders(
        self, monkeypatch, schema_tree
    ):
        _launch(monkeypatch, schema_tree, session=SID_A)
        first, second = run_state.list_sessions()
        assert first["slug"] == SLUG
        assert first["name"] is None
        assert first["status"] == "complete"
        assert first["phase"] == "implement"
        assert first["created"] == "2026-08-24T10:15:00Z"
        assert first["updated"] == "2026-08-24T10:41:00Z"
        assert second["name"] == "second-look"
        assert second["id"] == schema_tree.session_ids[1]
        assert second["status"] == "in-progress"

    def test_a_finished_session_is_never_orphaned(self, monkeypatch, schema_tree):
        _launch(monkeypatch, schema_tree, session=SID_A)
        first, second = run_state.list_sessions()
        assert first["orphaned"] is False
        # in-progress with no live owner: resumable, never auto-ended
        assert second["owner"] is None
        assert second["orphaned"] is True

    def test_a_live_owner_is_not_orphaned(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, state = run_state.ensure_run()
        state["owner"] = {"session_id": SID_A, "claimed_at": "2026-08-25T12:00:00Z"}
        run_state.write_state(rdir, state)
        (row,) = run_state.list_sessions(now="2026-08-25T12:30:00Z")
        assert row["owner"]["session_id"] == SID_A
        assert row["orphaned"] is False

    def test_a_stale_owner_is_orphaned(self, monkeypatch, tmp_path):
        """The claim is stale past the same window `claim_status` uses — the
        harness died without clearing it."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        rdir, state = run_state.ensure_run()
        state["owner"] = {"session_id": SID_A, "claimed_at": "2026-08-25T12:00:00Z"}
        run_state.write_state(rdir, state)
        (row,) = run_state.list_sessions(now="2026-08-26T12:00:00Z")
        assert row["orphaned"] is True

    def test_an_unreadable_record_is_listed_not_hidden(self, monkeypatch, tmp_path):
        """A record whose state cannot be read is still a record; dropping it
        is how a live run disappears from the operator's view."""
        _empty_schema_tree(monkeypatch, tmp_path, session=SID_A)
        base = run_state.runs_base()
        broken = base / "develop-issue-12" / SID_A
        broken.mkdir(parents=True)
        (broken / run_state.STATE_FILE).write_text("{[bad", encoding="utf-8")
        (row,) = run_state.list_sessions()
        assert row["id"] == SID_A
        assert row["slug"] is None
        assert row["status"] is None
        assert row["orphaned"] is None  # no verdict, not a verdict of "fine"

    def test_a_legacy_tree_lists_its_run_dirs(self, monkeypatch, legacy_tree):
        """`work sessions` answers "what is running" in both forms. A legacy
        run dir is not one launch's record — several sessions interleave in it
        — so it has no session id to report."""
        _launch(monkeypatch, legacy_tree, session="s-legacy-1")
        (row,) = run_state.list_sessions()
        assert row["id"] is None
        assert row["dir_name"] == SLUG
        assert row["slug"] == SLUG
        assert row["status"] == "in-progress"
        assert row["orphaned"] is True

    def test_no_run_context_lists_nothing(self, monkeypatch, tmp_path):
        _set_context(monkeypatch, tmp_path)
        assert run_state.list_sessions() == []

    def test_an_explicit_base_is_listed(self, monkeypatch, tmp_path, schema_tree):
        """Callers that hand a base in (a mirror walk) are unaffected by the
        form of the session's own tree."""
        _set_context(monkeypatch, tmp_path)
        rows = run_state.list_sessions(base=schema_tree.runs_dir)
        assert [row["id"] for row in rows] == list(schema_tree.session_ids)


# ---------------------------------------------------------------------------
# The record walk, shared
# ---------------------------------------------------------------------------


class TestIterRecordDirs:
    """One walk, both shapes: `iter_record_dirs` is where "which directories
    are records" is decided, so the resolvers, the listing and every later
    caller meet the same set."""

    def test_it_finds_session_dirs_in_a_schema_tree(self, monkeypatch, schema_tree):
        _launch(monkeypatch, schema_tree, session=SID_A)
        assert run_state.iter_record_dirs() == list(schema_tree.session_dirs)

    def test_it_finds_run_dirs_in_a_legacy_tree(self, monkeypatch, legacy_tree):
        _launch(monkeypatch, legacy_tree, session="s-legacy-1")
        assert run_state.iter_record_dirs() == list(legacy_tree.session_dirs)

    def test_it_never_descends_into_a_record(self, monkeypatch, legacy_tree):
        """A state-bearing dir IS the record — whatever is inside it is the
        run's own content (a bundle, a checkout), never a second record."""
        _launch(monkeypatch, legacy_tree, session="s-legacy-1")
        record = legacy_tree.session_dirs[0]
        _write_record(record / "masterplan" / "nested", "not-a-run")
        assert run_state.iter_record_dirs() == [record]

    def test_no_base_is_no_records(self, monkeypatch, tmp_path):
        _set_context(monkeypatch, tmp_path)
        assert run_state.iter_record_dirs() == []


class TestAFailedSeedLeavesNoGroupBehindEither:
    """The same defect as the re-slug husk, reached through the other door.

    `seed_run_dir` has the identical mkdir-then-rename shape ten lines above
    the re-slug path, and left the identical empty group when the rename
    failed. It was reported to me as out of scope on the grounds that a failed
    seed is "fatal and loud" where a failed re-slug drifts silently — true of
    the seed's own error, and beside the point: the husk is what drifts, and it
    drifts the same way.

    `slug_available` counts a bare directory as the slug being taken, and
    `release_run.unique_release_slug` consults it — so a seed that failed once
    sends the NEXT release to a stamped address (`<slug>-v1.2.3-<utc>`) for a
    run that was never created. Fixing one of the two and reporting the class
    closed is the mistake this run has already made twice.
    """

    @staticmethod
    def _launch(monkeypatch, tree):
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, tree.root, host=host, project=path,
                     session="20260824-101500-1-11111")

    def test_a_failed_seed_leaves_the_address_free(self, schema_tree, monkeypatch):
        self._launch(monkeypatch, schema_tree)
        area = schema_tree.runs_dir
        before = sorted(p.name for p in area.iterdir())

        with pytest.MonkeyPatch.context() as fail:
            fail.setattr(Path, "rename", _rename_refused)
            with pytest.raises(run_state.RunStateError, match="cannot seed run"):
                run_state.seed_run_dir("brand-new-slug", "release", "main")

        assert sorted(p.name for p in area.iterdir()) == before, (
            "a failed seed left its group behind"
        )
        assert run_state.slug_available("brand-new-slug", area), (
            "the address a seed reached for and never filled still reads as taken, "
            "so the next release run drifts to a stamped address"
        )

    def test_a_failed_seed_never_removes_a_group_holding_a_record(
        self, schema_tree, monkeypatch
    ):
        """The guard on the unwind: a group that was already there, holding
        another launch's record, is not this call's to remove."""
        self._launch(monkeypatch, schema_tree)
        area = schema_tree.runs_dir
        group = area / "shared-slug"
        _write_record(group / "20260101-000000-9-99999", "shared-slug")

        with pytest.MonkeyPatch.context() as fail:
            fail.setattr(Path, "rename", _rename_refused)
            with pytest.raises(run_state.RunStateError):
                run_state.seed_run_dir("shared-slug", "release", "main")

        assert (group / "20260101-000000-9-99999" / run_state.STATE_FILE).is_file(), (
            "the failed seed took away another launch's record"
        )

    def test_a_successful_seed_is_unchanged(self, schema_tree, monkeypatch):
        """The converse: refusing to leave a husk must not stop the ordinary
        seed, which is every seed."""
        self._launch(monkeypatch, schema_tree)
        rdir, state = run_state.seed_run_dir("brand-new-slug", "release", "main")

        assert (rdir / run_state.STATE_FILE).is_file()
        assert state["slug"] == "brand-new-slug"
        assert rdir.parent.name == "brand-new-slug"
