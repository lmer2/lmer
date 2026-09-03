"""The masterplan bundle root follows the SESSION dir (issue #353, D12/D22).

``MASTERPLAN_RUNS_DIR`` is what puts a masterplan bundle inside the lmer run
record instead of the code repository, and
:func:`lmer_cli.container.masterplan.masterplan_runs_dir` composes it as
``run_state.run_dir() / "masterplan"``. In schema form the run dir IS the
session dir (D22), so the bundle root moves with it — one bundle per launch,
flat, no ``<mp-slug>`` level (D12).

That is a claim about a composition, which is exactly the kind of thing that
holds until someone re-derives one of its halves. So these tests do not assert
that the module was left alone; they assert the ADDRESS it produces against a
real schema-form tree (``tests/plexus_fixtures``: two launches under one
``_runs/<slug>/`` group, the second one named), and against the legacy twin,
where the answer must be byte-identical to what it always was.

Three properties beyond the address itself:

- **The grouping level stays empty.** ``_runs/<slug>/`` is a grouping folder
  (D22) — a bundle root resolved there would be the one write at the level
  nothing may write at.
- **Two launches, two bundles.** The multi-bundle problem the de-nesting
  retires cannot come back through this function: each session id addresses
  its own dir.
- **The bundle is opaque** (D12). Resolving its root reads the record's state
  files, never the bundle: a bundle is a foreign writer's tree, and this
  helper's job is to name it, not to look inside. Pinned with a recorder
  around a bundle that has real content in it.

The exit-code contract with the shell caller (0 + bundle root / 1 not a
masterplan session / 2 enabled but indeterminate) is
``tests/test_masterplan_provisioning.py``'s; what is re-pinned here is that it
still reads that way when the tree is schema form.
"""

import builtins
import contextlib
from pathlib import Path

import pytest

from lmer_cli.container import masterplan
from tests import plexus_fixtures as pf
from tests.conftest import strip_lmer_env
from work_repo import run_state, utils


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context, no carried announcement, no carried session."""
    strip_lmer_env(monkeypatch)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    run_state._reset_synthesized_session_id()


def _launch(monkeypatch, tree, *, session=None, target=pf.TARGET):
    """The launch context a session runs with against *tree*'s project."""
    host, path = pf.PRIMARY_REPO.split("/", 1)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    monkeypatch.setenv("LMER_REPO_HOST", host)
    monkeypatch.setenv("LMER_REPO_PROJECT", path)
    monkeypatch.setenv("LMER_TASK", pf.TASKDEF)
    monkeypatch.setenv("LMER_TASK_TARGET", target)
    if session is None:
        monkeypatch.delenv("LMER_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("LMER_SESSION_ID", session)


# --- the address ------------------------------------------------------------


def test_the_bundle_root_is_the_launching_session_s_own(monkeypatch, schema_plexus):
    """`_runs/<slug>/<session-id>/masterplan`, not `_runs/<slug>/masterplan`."""
    _launch(monkeypatch, schema_plexus, session=pf.SESSION_IDS[0])

    root = masterplan.masterplan_runs_dir()

    assert root == schema_plexus.session_dirs[0] / "masterplan"
    assert root.parent.parent == schema_plexus.runs_dir / pf.SLUG


def test_a_named_session_keeps_its_bundle_after_the_rename(
    monkeypatch, schema_plexus
):
    """The freeze rename appends `--<name>` to the SESSION dir, and the bundle
    root is inside it: a launch that names itself mid-run must not go on
    quoting the address it had before, or the second half of its run writes a
    bundle beside the first."""
    _launch(monkeypatch, schema_plexus, session=pf.SESSION_IDS[1])

    root = masterplan.masterplan_runs_dir()

    assert root.parent.name == f"{pf.SESSION_IDS[1]}--{pf.SESSION_NAME}"
    assert root == schema_plexus.session_dirs[1] / "masterplan"


def test_two_launches_on_one_target_get_two_bundles(monkeypatch, schema_plexus):
    """D12's "at most one bundle per session dir", from the writer's side.

    Two people chatting about one issue are two session dirs (D22), so the
    thing that used to make several bundles share a run dir — one address per
    (taskdef, target) — cannot arise: the address carries the launch.
    """
    roots = []
    for session in pf.SESSION_IDS:
        _launch(monkeypatch, schema_plexus, session=session)
        roots.append(masterplan.masterplan_runs_dir())

    assert roots[0] != roots[1]
    assert {root.parent for root in roots} == set(schema_plexus.session_dirs)
    assert all(root.name == "masterplan" for root in roots)


def test_a_launch_with_no_record_yet_addresses_its_own_session_dir(
    monkeypatch, schema_plexus
):
    """A third launch against the same target: the bundle root is where its
    record WILL be, under the same group, and nothing is created by asking."""
    fresh = "20260825-090000-9-99999"
    _launch(monkeypatch, schema_plexus, session=fresh)

    root = masterplan.masterplan_runs_dir()

    assert root == schema_plexus.runs_dir / pf.SLUG / fresh / "masterplan"
    assert not root.exists(), "resolving an address must not create it"
    assert not root.parent.exists()


def test_nothing_is_addressed_at_the_grouping_level(monkeypatch, schema_plexus):
    """`_runs/<slug>/` is a grouping folder — tools MUST NOT write there
    (docs/SCHEMA.md, "Sessions and runs"). A bundle root resolved at that
    level would be the one write that breaks it, and it is also the pre-D22
    answer, so this is the regression the whole task is about."""
    group = schema_plexus.runs_dir / pf.SLUG
    for session in (None, pf.SESSION_IDS[0], "20260825-090000-9-99999"):
        _launch(monkeypatch, schema_plexus, session=session)

        root = masterplan.masterplan_runs_dir()

        assert root != group / "masterplan"
        assert root.parent.parent == group


def test_the_bundle_is_flat_under_the_session_dir(monkeypatch, schema_plexus):
    """D12: `<session-dir>/masterplan/`, with no `<mp-slug>` level between."""
    _launch(monkeypatch, schema_plexus, session=pf.SESSION_IDS[0])

    root = masterplan.masterplan_runs_dir()

    rel = root.relative_to(schema_plexus.runs_dir).parts
    assert rel == (pf.SLUG, pf.SESSION_DIR_NAMES[0], "masterplan")


def test_a_contextless_invocation_joins_the_group_s_newest_launch(
    monkeypatch, schema_plexus
):
    """No `LMER_SESSION_ID` (the mid-session `--force` path is one) addresses
    the newest session rather than inventing a launch — the same rule
    `run_state.run_dir()` applies to every other file in the record, so the
    bundle cannot end up in a dir the rest of the run never writes to."""
    _launch(monkeypatch, schema_plexus, session=None)

    root = masterplan.masterplan_runs_dir()

    assert root == schema_plexus.session_dirs[-1] / "masterplan"


# --- the legacy twin --------------------------------------------------------


def test_a_legacy_tree_gets_the_run_dir_it_always_did(monkeypatch, legacy_plexus):
    """`{host}/{project}/runs/<slug>/masterplan`, session id or not: a legacy
    tree has one shared run dir per target and is never restructured."""
    for session in (None, pf.SESSION_IDS[0]):
        _launch(monkeypatch, legacy_plexus, session=session)

        root = masterplan.masterplan_runs_dir()

        assert root == legacy_plexus.runs_dir / pf.SLUG / "masterplan"


def test_no_repo_context_is_still_no_bundle_root(monkeypatch, tmp_path):
    """Unchanged refusal: a chat-style session with nothing to resolve gets
    None, which is the caller's exit 2 (asked for masterplan, got nothing)."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    for var in ("LMER_REPO_HOST", "LMER_REPO_PROJECT", "LMER_REPO_URL"):
        monkeypatch.delenv(var, raising=False)

    assert masterplan.masterplan_runs_dir() is None


# --- the bundle stays opaque (D12) -----------------------------------------


class _Recorder:
    """Records every directory listing and every file read, by wrapping the
    calls a resolver would have to make to walk into a bundle."""

    def __init__(self):
        self.listed = []
        self.read = []

    @contextlib.contextmanager
    def recording(self):
        real_iterdir = Path.iterdir
        real_glob = Path.glob
        real_read_text = Path.read_text
        real_open = builtins.open

        def iterdir(this):
            self.listed.append(Path(this))
            return real_iterdir(this)

        def glob(this, pattern, *args, **kwargs):
            self.listed.append(Path(this))
            return real_glob(this, pattern, *args, **kwargs)

        def read_text(this, *args, **kwargs):
            self.read.append(Path(this))
            return real_read_text(this, *args, **kwargs)

        def opened(file, *args, **kwargs):
            self.read.append(Path(file))
            return real_open(file, *args, **kwargs)

        Path.iterdir = iterdir
        Path.glob = glob
        Path.read_text = read_text
        builtins.open = opened
        try:
            yield self
        finally:
            Path.iterdir = real_iterdir
            Path.glob = real_glob
            Path.read_text = real_read_text
            builtins.open = real_open

    def touched(self, base):
        """Every recorded path at or under *base*."""
        base = Path(base)
        return sorted(
            path
            for path in self.listed + self.read
            if path == base or base in path.parents
        )


def _plant_bundle(session_dir):
    """A bundle with real content in it: the thing a walk would find."""
    bundle = session_dir / "masterplan"
    (bundle / "handoff").mkdir(parents=True)
    (bundle / "spec.md").write_text("# spec\n", encoding="utf-8")
    (bundle / "handoff" / "brief.md").write_text("# brief\n", encoding="utf-8")
    return bundle


def test_resolving_the_bundle_root_never_looks_inside_it(
    monkeypatch, schema_plexus
):
    """A bundle is a foreign writer's tree, validated no further than its
    existence (D12) — and this helper does not even ask that much. It names
    an address; whether anything is there is masterplan's business.

    The bundle planted here HAS content, so a walk into it would be recorded:
    the assertion fails if the composition ever grows a "find the existing
    bundle" step, which is how the multi-bundle `<mp-slug>` scan came about.
    """
    bundle = _plant_bundle(schema_plexus.session_dirs[0])
    _launch(monkeypatch, schema_plexus, session=pf.SESSION_IDS[0])
    recorder = _Recorder()

    with recorder.recording():
        root = masterplan.masterplan_runs_dir()

    assert root == bundle
    assert recorder.listed, "the recorder saw nothing at all — it is not installed"
    assert recorder.touched(bundle) == [], "the bundle was walked into"


# --- the shell caller's contract, in schema form ----------------------------


def test_main_prints_the_session_bundle_root(monkeypatch, schema_plexus, capsys):
    """Exit 0 and exactly one line: the address the runner exports as
    MASTERPLAN_RUNS_DIR."""
    _launch(monkeypatch, schema_plexus, session=pf.SESSION_IDS[0])
    monkeypatch.setenv("LMER_MASTERPLAN", "1")

    rc = masterplan.main([])

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == [str(schema_plexus.session_dirs[0] / "masterplan")]


def test_main_is_still_silent_when_the_session_is_not_one(
    monkeypatch, schema_plexus, capsys
):
    """Exit 1, no output: a schema-form tree does not make every session a
    masterplan session."""
    _launch(monkeypatch, schema_plexus, session=pf.SESSION_IDS[0])
    monkeypatch.delenv("LMER_TASK", raising=False)
    monkeypatch.delenv("LMER_MASTERPLAN", raising=False)
    monkeypatch.setenv("LMER_TASKDEF_PATHS", str(schema_plexus.root / "no-taskdefs"))

    rc = masterplan.main([])

    assert rc == 1
    assert capsys.readouterr().out == ""


def test_main_warns_rather_than_skipping_when_the_tree_says_nothing(
    monkeypatch, tmp_path, capsys
):
    """Exit 2 (enabled but indeterminate) still separates "asked for it and
    got nothing" from "never asked" — the distinction claude-runner.sh warns
    on."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_MASTERPLAN", "1")
    for var in ("LMER_REPO_HOST", "LMER_REPO_PROJECT", "LMER_REPO_URL", "LMER_TASK"):
        monkeypatch.delenv(var, raising=False)

    rc = masterplan.main([])

    assert rc == 2
    assert capsys.readouterr().out == ""
