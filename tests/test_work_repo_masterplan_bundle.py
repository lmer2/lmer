"""The masterplan bundle sync, by form (issue #353, D12/D22).

`run_state.sync_masterplan_artifacts` surfaces a bundle's well-known
artifacts (`spec.md`, `goals.md`, `plan.md`, `plan.html`, `retro.md`) as
relative symlinks at the record root, registers each one in
`state.artifacts` through the single writer, and indexes the spec-class ones
in the project's specs area — pointing at the BUNDLE file, never at the
convenience symlink it just made (docs/RUN-STATE.md, "Masterplan artifact
links").

What #353 changes is WHERE the bundle is, and nothing else. In schema form
the record is the session dir (D22) and one launch owns one session dir, so
the bundle is flat — `<session-dir>/masterplan/<artifact>` — with no
`<mp-slug>` level to iterate and therefore nothing to disambiguate: the
multi-bundle `<mp-slug>-<name>` link prefix retires with the nesting
(docs/SCHEMA.md Appendix A, and the `<session-dir>/masterplan/` inventory
row: "flat bundle, at most one per session directory"). A legacy
`runs/<slug>/` dir is still one shared dir per target, several bundles and
all, so it keeps iterating `masterplan/<mp-slug>/` exactly as it always did.

Three properties beyond the addresses:

- **The bundle stays opaque** (D12). It is a foreign writer's tree, so the
  flat lookup asks for the five names it knows and never LISTS the bundle —
  pinned with a call recorder around a bundle that has real content in it,
  the same instrument `tests/test_masterplan_runs_dir_session.py` uses on the
  writer's side of the same contract.
- **The links satisfy the validator's own shape rules.** `index.link-not-relative`
  and `index.link-escapes` are ERROR tier in every form, so every link this
  module writes — the session-root artifact links and the specs entries — is
  put through the shipped checker rather than eyeballed here.
- **Legacy is byte-identical**, asserted against a verbatim copy of the
  pre-change implementation (`_reference_sync_masterplan_artifacts`) run on
  an identical tree, not against a hand-written expectation.

The legacy behaviours themselves (naming, replacement, registration,
fail-soft) keep their home in
`tests/test_work_repo_run_state.py::TestSyncMasterplanArtifacts`, which runs
in a manifest-less tree and is untouched by this task.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lmer_schema.checks import Form, Report
from lmer_schema.checks.index_links import check_index_links
from lmer_schema.checks.record import check_record
from tests import plexus_fixtures as pf
from tests.conftest import strip_lmer_env
from tests.test_masterplan_runs_dir_session import _Recorder
from work_repo import run_state, specs_index, utils

#: The instant staleness is measured against, so this file's verdicts do not
#: depend on the wall clock: the fixture's sessions are stamped 2026-08-24, and
#: `record.session-stale` fires 14 days after that.
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context, no carried announcement, no carried session."""
    strip_lmer_env(monkeypatch)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    run_state._reset_synthesized_session_id()


def _launch(monkeypatch, root, *, session=None, host=None, project=None):
    """The launch context a session of *root*'s registered project runs with."""
    if host is None or project is None:
        host, project = pf.PRIMARY_REPO.split("/", 1)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv("LMER_REPO_HOST", host)
    monkeypatch.setenv("LMER_REPO_PROJECT", project)
    monkeypatch.setenv("LMER_TASK", pf.TASKDEF)
    monkeypatch.setenv("LMER_TASK_TARGET", pf.TARGET)
    if session is None:
        monkeypatch.delenv("LMER_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("LMER_SESSION_ID", session)


def _flat_bundle(rdir, *names):
    """A D12 bundle: `<record>/masterplan/<artifact>`, no `<mp-slug>` level.

    Content beside the artifacts (a `handoff/` subdir, a file no link ever
    names) is what a real bundle looks like and what a walk would find.
    """
    bundle = rdir / "masterplan"
    (bundle / "handoff").mkdir(parents=True, exist_ok=True)
    (bundle / "handoff" / "brief.md").write_text("# brief\n", encoding="utf-8")
    for name in names:
        (bundle / name).write_text(f"flat:{name}\n", encoding="utf-8")
    return bundle


def _nested_bundle(rdir, mp_slug, *names):
    """A legacy bundle: `<run>/masterplan/<mp-slug>/<artifact>`."""
    bundle = rdir / "masterplan" / mp_slug
    bundle.mkdir(parents=True, exist_ok=True)
    for name in names:
        (bundle / name).write_text(f"{mp_slug}:{name}\n", encoding="utf-8")
    return bundle


def _links(rdir):
    """`{name: raw symlink target}` for the record root — what the sync makes."""
    return {
        p.name: os.readlink(p).replace(os.sep, "/")
        for p in sorted(rdir.iterdir())
        if p.is_symlink()
    }


def _entries(sdir):
    """`{entry name: raw symlink target}` for a specs index area."""
    if sdir is None or not Path(sdir).is_dir():
        return {}
    return {
        p.name: os.readlink(p).replace(os.sep, "/")
        for p in sorted(Path(sdir).iterdir())
        if p.is_symlink()
    }


# ===========================================================================
# Schema form: the bundle is flat
# ===========================================================================


class TestTheBundleIsFlatInSchemaForm:
    """`<session-dir>/masterplan/<artifact>` — one bundle per session dir,
    because the session dir IS the fragment (D12 over D22)."""

    def test_present_artifacts_link_under_plain_names(
        self, monkeypatch, schema_plexus
    ):
        rdir = schema_plexus.session_dirs[1]
        _flat_bundle(rdir, "spec.md", "plan.md")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])

        linked = run_state.sync_masterplan_artifacts(rdir)

        assert linked == ["spec.md", "plan.md"]
        for absent in ("goals.md", "plan.html", "retro.md"):
            assert not (rdir / absent).exists()

    def test_the_targets_reach_the_bundle_one_level_down(
        self, monkeypatch, schema_plexus
    ):
        """`masterplan/<artifact>`, not `masterplan/<mp-slug>/<artifact>`."""
        rdir = schema_plexus.session_dirs[1]
        _flat_bundle(rdir, "spec.md", "retro.md")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])

        run_state.sync_masterplan_artifacts(rdir)

        assert _links(rdir) == {
            "spec.md": "masterplan/spec.md",
            "retro.md": "masterplan/retro.md",
        }
        assert (rdir / "spec.md").read_text() == "flat:spec.md\n"

    def test_no_mp_slug_level_is_iterated(self, monkeypatch, schema_plexus):
        """The de-nesting, from the reader's side: a `<mp-slug>` dir under a
        SESSION dir's `masterplan/` is bundle internals — a foreign writer's
        tree (D12) — and the sync neither links out of it nor invents the
        `<mp-slug>-<name>` prefix that retired with the nesting."""
        rdir = schema_plexus.session_dirs[1]
        _nested_bundle(rdir, "mp-a", "spec.md", "plan.md")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])

        assert run_state.sync_masterplan_artifacts(rdir) == []
        assert _links(rdir) == {}
        assert _entries(specs_index.specs_dir()) == _entries(
            schema_plexus.specs_dir
        )  # the fixture's own entry, nothing added

    def test_the_bundle_is_never_listed_or_read(self, monkeypatch, schema_plexus):
        """Opacity is a property of HOW the artifacts are found: the flat
        lookup asks for the five names it knows, so no bundle is ever walked.
        The bundle planted here has content beside its artifacts, so a walk
        would be recorded — which is what fails if the discovery ever grows a
        "find the bundle" step back (the `<mp-slug>` scan is exactly that)."""
        rdir = schema_plexus.session_dirs[1]
        bundle = _flat_bundle(rdir, "spec.md")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])
        recorder = _Recorder()

        with recorder.recording():
            linked = run_state.sync_masterplan_artifacts(rdir)

        assert linked == ["spec.md"]
        assert recorder.listed, "the recorder saw nothing at all — it is not installed"
        assert recorder.touched(bundle) == [], "the bundle was walked into"

    def test_a_regular_file_is_still_replaced_by_the_link(
        self, monkeypatch, schema_plexus
    ):
        """The fixture's first session carries a real `spec.md` file; the
        bundle copy is canonical in either form."""
        rdir = schema_plexus.session_dirs[0]
        assert (rdir / "spec.md").is_file() and not (rdir / "spec.md").is_symlink()
        _flat_bundle(rdir, "spec.md")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[0])

        assert run_state.sync_masterplan_artifacts(rdir) == ["spec.md"]

        assert (rdir / "spec.md").is_symlink()
        assert (rdir / "spec.md").read_text() == "flat:spec.md\n"

    def test_the_links_register_through_the_single_writer(
        self, monkeypatch, schema_plexus
    ):
        """`state.artifacts`, keyed by stem like `work artifact` — written by
        `write_state` and by nothing else, and only when it changes."""
        rdir = schema_plexus.session_dirs[1]
        _flat_bundle(rdir, "spec.md", "plan.md", "plan.html")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])

        run_state.sync_masterplan_artifacts(rdir)

        artifacts = run_state.load_state(rdir)["artifacts"]
        assert artifacts["spec"] == "spec.md"
        assert artifacts["plan"] == "plan.md"
        assert artifacts["plan.html"] == "plan.html"

        writes = []
        monkeypatch.setattr(run_state, "write_state", lambda *a, **k: writes.append(a))
        assert run_state.sync_masterplan_artifacts(rdir) == [
            "spec.md", "plan.md", "plan.html",
        ]
        assert writes == []  # idempotent: nothing changed, no second write

    def test_the_specs_entry_points_at_the_bundle_file(
        self, monkeypatch, schema_plexus
    ):
        """The one canonical target, one level deeper than a legacy tree's
        (D22) and one level shallower inside the bundle than before (D12) —
        never the run-root convenience symlink this sync just made."""
        rdir = schema_plexus.session_dirs[1]
        _flat_bundle(rdir, "spec.md")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])
        before = _entries(schema_plexus.specs_dir)

        run_state.sync_masterplan_artifacts(rdir)

        after = _entries(schema_plexus.specs_dir)
        added = {name: after[name] for name in set(after) - set(before)}
        assert list(added.values()) == [
            f"../_runs/{pf.SLUG}/{pf.SESSION_DIR_NAMES[1]}/masterplan/spec.md"
        ]
        assert specs_index.specs_dir() == schema_plexus.specs_dir

    def test_the_grouping_level_is_never_written(self, monkeypatch, schema_plexus):
        """`_runs/<slug>/` is a grouping folder and NOTHING is written there
        (D22) — including by a sync run against a session inside it."""
        group = schema_plexus.runs_dir / pf.SLUG
        before = sorted(p.name for p in group.iterdir())
        rdir = schema_plexus.session_dirs[1]
        _flat_bundle(rdir, "spec.md")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])

        run_state.sync_masterplan_artifacts(rdir)

        assert sorted(p.name for p in group.iterdir()) == before

    def test_an_empty_bundle_links_nothing(self, monkeypatch, schema_plexus):
        rdir = schema_plexus.session_dirs[1]
        (rdir / "masterplan").mkdir()
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])

        assert run_state.sync_masterplan_artifacts(rdir) == []
        assert _links(rdir) == {}

    def test_a_session_without_a_bundle_is_untouched(
        self, monkeypatch, schema_plexus
    ):
        rdir = schema_plexus.session_dirs[1]
        before = sorted(p.name for p in rdir.iterdir())
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])

        assert run_state.sync_masterplan_artifacts(rdir) == []
        assert sorted(p.name for p in rdir.iterdir()) == before

    def test_an_unusable_manifest_does_not_demote_the_bundle_to_the_old_shape(
        self, monkeypatch, tmp_path
    ):
        """The routing question is the FORM's, not the registry's: a tree with
        a present-but-broken `_plexus.yaml` is a schema tree that cannot be
        read (`utils.work_repo_layout()`), so its records are session dirs and
        its bundles are flat. Reading such a tree at the legacy nesting would
        be the split-form record the validator flags. The specs index has no
        base to write into there and skips itself, which is its own contract.
        """
        root = tmp_path / "plexus-broken"
        rdir = root / "lmer" / "_runs" / pf.SLUG / pf.SESSION_IDS[0]
        rdir.mkdir(parents=True)
        (root / "_plexus.yaml").write_text("schema: 1\nprojects: [", encoding="utf-8")
        _flat_bundle(rdir, "spec.md")
        _nested_bundle(rdir, "mp-a", "plan.md")
        _launch(monkeypatch, root, session=pf.SESSION_IDS[0])

        assert run_state.sync_masterplan_artifacts(rdir) == ["spec.md"]
        assert _links(rdir) == {"spec.md": "masterplan/spec.md"}

    def test_a_symlink_failure_is_still_fail_soft(
        self, monkeypatch, schema_plexus, capsys
    ):
        """Fail-soft everywhere: a sync problem is reported and skipped, and
        never fails the `work commit` that triggered it."""
        rdir = schema_plexus.session_dirs[1]
        _flat_bundle(rdir, "spec.md")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[1])
        monkeypatch.setattr(
            Path, "symlink_to",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no symlinks here")),
        )

        assert run_state.sync_masterplan_artifacts(rdir) == []  # must not raise

        assert "skipped spec.md" in capsys.readouterr().out
        assert run_state.load_state(rdir)["artifacts"] == {}


class TestTheValidatorAcceptsEveryLinkThisSyncWrites:
    """`index.link-not-relative` and `index.link-escapes` are ERROR tier in
    every form (docs/SCHEMA.md), and a session-root artifact link that
    dangles is an error too — so the links are checked with the shipped
    checker rather than against this test's idea of a relative path.

    `check_index_links` covers the specs entries (it walks index dirs) and
    `check_record` covers the session dir's own artifact links — the shipped
    path, so this asserts what `lmer-schema validate` would say rather than
    what a hand-called primitive would.

    It used to call `check_symlinks` by hand, noting that no check walked a
    session dir. That was true when this was written and is no longer: !261
    gave `docs/SCHEMA.md`'s artifact-symlink row a check in
    `checks/record.py`, so the hand-call would now be testing a path the
    validator does not take.
    """

    @pytest.mark.parametrize("which", [0, 1])
    def test_no_findings_for_a_synced_session(
        self, monkeypatch, schema_plexus, which
    ):
        rdir = schema_plexus.session_dirs[which]
        _flat_bundle(rdir, "spec.md", "goals.md", "plan.md", "plan.html", "retro.md")
        _launch(monkeypatch, schema_plexus.root, session=pf.SESSION_IDS[which])

        linked = run_state.sync_masterplan_artifacts(rdir)

        assert len(linked) == 5  # something was actually checked
        assert _entries(schema_plexus.specs_dir)
        report = Report(Form.SCHEMA)
        check_index_links(schema_plexus.root, report)
        check_record(schema_plexus.root, report, now=NOW)
        assert [(f.code, f.path) for f in report.findings] == []


# ===========================================================================
# Legacy form: byte-identical, against the pre-change implementation
# ===========================================================================


def _reference_sync_masterplan_artifacts(rdir):
    """`sync_masterplan_artifacts()` VERBATIM before this task (run_state.py
    2377-2469 at the time): the `masterplan/<mp-slug>/` iteration, the
    multi-bundle prefixing and the alias passed to the specs index.

    The helpers it calls (`load_state`, `write_state`, `specs_index`) are the
    shipped ones — this task does not touch them, and a copy would drift from
    the suite instead of pinning it.
    """
    linked = []
    spec_files = []
    try:
        mp_dir = rdir / run_state.MASTERPLAN_DIR
        if not mp_dir.is_dir():
            return []
        bundles = sorted(
            (p for p in mp_dir.iterdir() if p.is_dir()), key=lambda p: p.name
        )
        prefixed = len(bundles) > 1
        for bundle in bundles:
            for name in run_state.MASTERPLAN_ARTIFACTS:
                if not (bundle / name).is_file():
                    continue
                link_name = f"{bundle.name}-{name}" if prefixed else name
                if specs_index.is_spec_artifact(name):
                    spec_files.append((bundle / name, link_name))
                target = os.path.join(run_state.MASTERPLAN_DIR, bundle.name, name)
                link_path = rdir / link_name
                try:
                    if link_path.is_symlink():
                        if os.readlink(link_path) == target:
                            linked.append(link_name)
                            continue
                        link_path.unlink()
                    elif link_path.exists():
                        link_path.unlink()
                    link_path.symlink_to(target)
                    linked.append(link_name)
                except OSError as exc:
                    print(f"⚠️  masterplan artifact sync: skipped {link_name}: {exc}")
        if linked:
            try:
                state = run_state.load_state(rdir)
                if state is not None:
                    artifacts = dict(state.get("artifacts") or {})
                    changed = False
                    assigned = {}
                    for link_name in linked:
                        key = Path(link_name).stem
                        if assigned.get(key, link_name) != link_name:
                            key = link_name
                        assigned[key] = link_name
                        if artifacts.get(key) != link_name:
                            artifacts[key] = link_name
                            changed = True
                    if changed:
                        state["artifacts"] = artifacts
                        run_state.write_state(rdir, state)
            except Exception as exc:
                print(f"⚠️  masterplan artifact sync: could not register in state: {exc}")
        for spec_file, link_name in spec_files:
            specs_index.upsert_spec_link(
                spec_file, specs_index.run_label(rdir), alias=link_name
            )
    except Exception as exc:
        print(f"⚠️  masterplan artifact sync skipped: {exc}")
    return linked


#: One legacy run dir per case, populated identically for both implementations.
#: Each entry is (case name, populate(rdir)).
def _case_one_bundle(rdir):
    _nested_bundle(rdir, "mp-a", "spec.md", "plan.md")


def _case_two_bundles(rdir):
    _nested_bundle(rdir, "mp-a", "spec.md")
    _nested_bundle(rdir, "mp-b", "spec.md", "plan.html")


def _case_stem_collision(rdir):
    _nested_bundle(rdir, "mp-a", "plan.md", "plan.html")


def _case_every_artifact(rdir):
    _nested_bundle(
        rdir, "mp-a", "spec.md", "goals.md", "plan.md", "plan.html", "retro.md"
    )


def _case_regular_file_in_the_way(rdir):
    _nested_bundle(rdir, "mp-a", "spec.md")
    (rdir / "spec.md").write_text("manually copied, stale\n", encoding="utf-8")


def _case_wrong_symlink_in_the_way(rdir):
    _nested_bundle(rdir, "mp-a", "spec.md")
    (rdir / "spec.md").symlink_to("somewhere/else.md")


def _case_flat_files_beside_the_bundles(rdir):
    """A `masterplan/` dir holding loose files as well as bundle dirs: the
    legacy reader takes the DIRS only, and must go on doing so — this is the
    shape the schema-form reader now takes the other half of."""
    _nested_bundle(rdir, "mp-a", "spec.md")
    (rdir / "masterplan" / "spec.md").write_text("loose\n", encoding="utf-8")
    (rdir / "masterplan" / "README.md").write_text("# bundle root\n", encoding="utf-8")


def _case_empty_bundle(rdir):
    (rdir / "masterplan" / "mp-a").mkdir(parents=True)


def _case_no_bundle(rdir):
    (rdir / "notes.md").write_text("# notes\n", encoding="utf-8")


LEGACY_CASES = [
    _case_one_bundle,
    _case_two_bundles,
    _case_stem_collision,
    _case_every_artifact,
    _case_regular_file_in_the_way,
    _case_wrong_symlink_in_the_way,
    _case_flat_files_beside_the_bundles,
    _case_empty_bundle,
    _case_no_bundle,
]


def _legacy_tree(root, *, seeded=True):
    """A manifest-less work repo with one run dir, at today's bare names."""
    host, project = pf.PRIMARY_REPO.split("/", 1)
    rdir = root / host / project / "runs" / pf.SLUG
    rdir.mkdir(parents=True)
    if seeded:
        run_state.write_state(
            rdir, run_state.seed_state(pf.SLUG, pf.TASKDEF, pf.TARGET)
        )
    return rdir


def _observable(rdir, sdir):
    """Everything the sync can be seen to have done: the links it made, the
    registry it wrote, and the index entries it left behind."""
    state = run_state._read_sibling_state(rdir)
    return {
        "links": _links(rdir),
        "files": sorted(p.name for p in rdir.iterdir() if p.is_file()),
        "artifacts": (state or {}).get("artifacts"),
        "entries": _entries(sdir),
    }


class TestALegacyRunDirIsUnchanged:
    """A legacy `runs/<slug>/` is one shared dir per target, so several
    bundles can sit under its `masterplan/` and the `<mp-slug>-<name>` prefix
    still earns its keep. Every case below runs the shipped implementation
    and the verbatim pre-change one over identical trees and compares
    everything either can be seen to have done.
    """

    @pytest.mark.parametrize(
        "populate", LEGACY_CASES, ids=[f.__name__[len("_case_"):] for f in LEGACY_CASES]
    )
    @pytest.mark.parametrize("seeded", [True, False], ids=["seeded", "no-state"])
    def test_matches_the_pre_change_implementation(
        self, monkeypatch, tmp_path, populate, seeded
    ):
        results = {}
        for which, sync in (
            ("shipped", run_state.sync_masterplan_artifacts),
            ("reference", _reference_sync_masterplan_artifacts),
        ):
            root = tmp_path / which
            rdir = _legacy_tree(root, seeded=seeded)
            populate(rdir)
            _launch(monkeypatch, root, session=pf.SESSION_IDS[0])
            returned = sync(rdir)
            results[which] = (returned, _observable(rdir, specs_index.specs_dir()))

        assert results["shipped"] == results["reference"]

    def test_the_oracle_and_the_shipped_path_both_did_something(
        self, monkeypatch, tmp_path
    ):
        """The comparison above is only worth anything if the two agree on a
        tree that produces links, entries and a state write — an oracle that
        returns `[]` everywhere would agree with anything."""
        root = tmp_path / "live"
        rdir = _legacy_tree(root)
        _case_two_bundles(rdir)
        _launch(monkeypatch, root, session=pf.SESSION_IDS[0])

        linked = run_state.sync_masterplan_artifacts(rdir)

        assert linked == ["mp-a-spec.md", "mp-b-spec.md", "mp-b-plan.html"]
        observed = _observable(rdir, specs_index.specs_dir())
        assert observed["links"]["mp-b-plan.html"] == "masterplan/mp-b/plan.html"
        assert observed["artifacts"]["mp-a-spec"] == "mp-a-spec.md"
        assert len(observed["entries"]) == 2  # one per bundle spec, kept distinct

    def test_a_legacy_tree_does_not_read_the_flat_shape(
        self, monkeypatch, tmp_path
    ):
        """The de-nesting is not retroactive: a legacy run dir is never
        restructured by schema-1 tooling (D22/§3.5), so a loose `spec.md`
        directly under its `masterplan/` is not a bundle artifact there."""
        root = tmp_path / "legacy"
        rdir = _legacy_tree(root)
        (rdir / "masterplan").mkdir()
        (rdir / "masterplan" / "spec.md").write_text("loose\n", encoding="utf-8")
        _launch(monkeypatch, root, session=pf.SESSION_IDS[0])

        assert run_state.sync_masterplan_artifacts(rdir) == []
        assert _links(rdir) == {}
