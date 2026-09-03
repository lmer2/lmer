"""Tests for the masterplan mirror candidate list in masterplan-enable.sh.

The lmer schema (D18) renames the work repo's `mirrors/` to `_agents/mirrors/`,
so the script must recognize BOTH spellings of the work-repo mirror while the
taskdef repo stays the canonical (first) candidate:

    /taskdef/mirrors/masterplan            canonical — no warning
    /work/_agents/mirrors/masterplan       schema-form work-repo fallback — warns
    /work/mirrors/masterplan               legacy work-repo fallback — warns, LAST

The work repo is deprecated as a plugin source in either spelling (the
deprecation is about which REPO, not the directory name), so both work-repo
entries carry the deprecation warning; the warning names which form was hit.
The legacy bare name stays last so the "no candidate exists → attempt the
last one blindly" fallthrough keeps landing where it always has.

LMER_MASTERPLAN_MIRROR_CANDIDATES is operator-settable, so a candidate can
be neither work-repo path (`/opt/mirrors/masterplan`, say). Such a custom
candidate is still reported when it is not the canonical (first) one, but
ONLY that -- no "legacy bare-name" label and no work-repo deprecation claim,
both of which would be false. The work-repo arms match the work repo
(`$LMER_WORK_REPO_PATH`) specifically, not any path ending in the same name.
The blind fallthrough (nothing exists) keeps pointing at LMER_TASKDEF_REPO
whatever the list: with no mirror anywhere, that IS the fix.

The default list is made of absolute container paths, so the resolution
tests drive LMER_MASTERPLAN_MIRROR_CANDIDATES with a tmp tree shaped exactly
like the default; a source guard pins the default string itself.
"""
import re
from pathlib import Path

from tests.test_masterplan_provisioning import MASTERPLAN_ENABLE, _run_enable_script

DEFAULT_CANDIDATES = (
    "/taskdef/mirrors/masterplan"
    ":/work/_agents/mirrors/masterplan"
    ":/work/mirrors/masterplan"
)


def _mirror_paths(tmp_path):
    """The three default candidates, re-rooted under tmp_path (same shape)."""
    return (
        tmp_path / "taskdef" / "mirrors" / "masterplan",
        tmp_path / "work" / "_agents" / "mirrors" / "masterplan",
        tmp_path / "work" / "mirrors" / "masterplan",
    )


def _env(tmp_path, candidates=None):
    """Forced-mode env with the candidate list mirroring the default shape.

    Nothing is created on disk here — each test creates exactly the mirror
    dirs whose presence it is asserting on.
    """
    if candidates is None:
        candidates = _mirror_paths(tmp_path)
    return {
        "LMER_WORK_REPO_PATH": str(tmp_path / "work"),
        "LMER_REPO_HOST": "git.example.com",
        "LMER_REPO_PROJECT": "group/proj",
        "LMER_MASTERPLAN_MIRROR_CANDIDATES": ":".join(str(c) for c in candidates),
    }


def _marketplace_add_target(calls):
    targets = [
        c.split("plugin marketplace add ", 1)[1]
        for c in calls
        if c.startswith("plugin marketplace add ")
    ]
    assert len(targets) == 1, f"expected exactly one marketplace add, got {calls!r}"
    return targets[0]


# ── Default list (source guard) ────────────────────────────────────────


def test_default_candidates_taskdef_then_schema_form_then_legacy():
    """Guard: the default keeps the taskdef repo first (canonical), adds the
    schema-form work-repo mirror, and leaves the legacy bare name LAST so
    the blind fallthrough is unchanged."""
    source = MASTERPLAN_ENABLE.read_text()
    match = re.search(
        r"""\$\{LMER_MASTERPLAN_MIRROR_CANDIDATES:-([^}]+)\}""", source
    )
    assert match, "default LMER_MASTERPLAN_MIRROR_CANDIDATES expansion not found"
    assert match.group(1) == DEFAULT_CANDIDATES


# ── Resolution over the default-shaped tree ────────────────────────────


def test_schema_form_work_mirror_chosen_and_warns(tmp_path):
    """Only `_agents/mirrors/masterplan` exists → it is used, but it is
    still the work repo, not the taskdef repo: the deprecation warning
    fires and says the schema-form mirror was hit."""
    taskdef, schema, _legacy = _mirror_paths(tmp_path)
    schema.mkdir(parents=True)
    rc, _out, err, calls = _run_enable_script(tmp_path, _env(tmp_path))
    assert rc == 0
    assert _marketplace_add_target(calls) == str(schema)
    assert "deprecated" in err and "LMER_TASKDEF_REPO" in err
    assert str(taskdef) in err, "warning must name the canonical candidate"
    assert "schema-form" in err
    assert "legacy" not in err


def test_legacy_work_mirror_chosen_and_warns_with_rename_hint(tmp_path):
    """Only the bare `mirrors/masterplan` exists → it is used (nothing
    breaks for an unmigrated tree), and the warning says which fallback was
    taken AND points at the D18 rename."""
    _taskdef, _schema, legacy = _mirror_paths(tmp_path)
    legacy.mkdir(parents=True)
    rc, _out, err, calls = _run_enable_script(tmp_path, _env(tmp_path))
    assert rc == 0
    assert _marketplace_add_target(calls) == str(legacy)
    assert "deprecated" in err and "LMER_TASKDEF_REPO" in err
    assert "legacy" in err
    assert "_agents/mirrors/" in err, "warning must point at the schema-form name"


def test_both_work_mirrors_present_schema_form_wins(tmp_path):
    """Both work-repo spellings exist (mid-migration) → schema form wins;
    the legacy name is only the last-resort fallthrough."""
    _taskdef, schema, legacy = _mirror_paths(tmp_path)
    schema.mkdir(parents=True)
    legacy.mkdir(parents=True)
    rc, _out, err, calls = _run_enable_script(tmp_path, _env(tmp_path))
    assert rc == 0
    assert _marketplace_add_target(calls) == str(schema)
    assert "schema-form" in err


def test_taskdef_mirror_wins_over_both_work_forms_without_warning(tmp_path):
    """The canonical (first) candidate beats both work-repo spellings and
    is the ONLY resolution that stays silent."""
    taskdef, schema, legacy = _mirror_paths(tmp_path)
    for path in (taskdef, schema, legacy):
        path.mkdir(parents=True)
    rc, _out, err, calls = _run_enable_script(tmp_path, _env(tmp_path))
    assert rc == 0
    assert _marketplace_add_target(calls) == str(taskdef)
    assert "deprecated" not in err


def test_no_mirror_present_falls_through_to_legacy_last_candidate(tmp_path):
    """None exist → the LAST candidate (legacy bare name) is attempted
    blindly, as before the schema-form entry was added; the warning says
    no candidate existed rather than mislabelling the path."""
    _taskdef, _schema, legacy = _mirror_paths(tmp_path)
    assert not legacy.exists()
    rc, _out, err, calls = _run_enable_script(tmp_path, _env(tmp_path))
    assert rc == 0
    assert _marketplace_add_target(calls) == str(legacy)
    assert "deprecated" in err and "LMER_TASKDEF_REPO" in err
    assert "no candidate exists" in err


def test_env_override_still_wins(tmp_path):
    """An explicit LMER_MASTERPLAN_MIRROR_CANDIDATES replaces the default
    list entirely — the default-shaped dirs on disk are ignored."""
    taskdef, schema, legacy = _mirror_paths(tmp_path)
    for path in (taskdef, schema, legacy):
        path.mkdir(parents=True)
    custom = tmp_path / "elsewhere" / "masterplan"
    custom.mkdir(parents=True)
    rc, _out, err, calls = _run_enable_script(tmp_path, _env(tmp_path, [custom]))
    assert rc == 0
    assert _marketplace_add_target(calls) == str(custom)
    # A single-entry override is its own canonical candidate: no warning.
    assert "deprecated" not in err
    for path in (taskdef, schema, legacy):
        assert str(path) not in "\n".join(calls)


def test_env_override_order_is_respected_over_default_order(tmp_path):
    """The override's ORDER is authoritative too: listing the legacy name
    first makes it canonical for that run (no warning, legacy chosen even
    though the schema-form dir exists)."""
    _taskdef, schema, legacy = _mirror_paths(tmp_path)
    schema.mkdir(parents=True)
    legacy.mkdir(parents=True)
    env = _env(tmp_path, [legacy, schema])
    rc, _out, err, calls = _run_enable_script(tmp_path, env)
    assert rc == 0
    assert _marketplace_add_target(calls) == str(legacy)
    assert "deprecated" not in err


# ── Custom (non-work-repo) candidates ──────────────────────────────────


def _custom_candidate(tmp_path):
    """A candidate outside the work repo whose LAST components spell the
    legacy bare name -- the shape that must NOT trip the work-repo arm."""
    return tmp_path / "elsewhere" / "mirrors" / "masterplan"


def test_custom_candidate_warns_only_that_it_is_not_canonical(tmp_path):
    """A non-canonical custom candidate is reported honestly: it names the
    resolved path and the canonical candidate, and nothing else."""
    taskdef, _schema, _legacy = _mirror_paths(tmp_path)
    custom = _custom_candidate(tmp_path)
    custom.mkdir(parents=True)
    rc, _out, err, calls = _run_enable_script(tmp_path, _env(tmp_path, [taskdef, custom]))
    assert rc == 0
    assert _marketplace_add_target(calls) == str(custom)
    assert f"resolved to {custom}" in err
    assert "not the canonical (first) candidate" in err
    assert str(taskdef) in err, "warning must name the canonical candidate"
    for claim in ("legacy", "bare-name", "_agents/mirrors/", "deprecated", "LMER_TASKDEF_REPO"):
        assert claim not in err, f"{claim!r} is a false claim for a custom candidate:\n{err}"


def test_custom_last_candidate_blind_fallthrough_is_not_mislabelled(tmp_path):
    """None exist and the LAST candidate is custom: the blind attempt is
    reported as such and still points at the fix (nothing exists anywhere,
    so LMER_TASKDEF_REPO is the answer whatever the list -- and
    test_masterplan_provisioning.py pins that), but the custom path is not
    labelled a legacy bare-name mirror."""
    taskdef, _schema, _legacy = _mirror_paths(tmp_path)
    custom = _custom_candidate(tmp_path)
    assert not custom.exists()
    rc, _out, err, calls = _run_enable_script(tmp_path, _env(tmp_path, [taskdef, custom]))
    assert rc == 0
    assert _marketplace_add_target(calls) == str(custom)
    assert "no candidate exists" in err and "LMER_TASKDEF_REPO" in err
    assert "legacy" not in err and "bare-name" not in err


def test_work_repo_arms_match_the_work_repo_path_specifically(tmp_path):
    """The same two default spellings rooted OUTSIDE the work repo are
    custom candidates, not work-repo ones."""
    taskdef, _schema, _legacy = _mirror_paths(tmp_path)
    other_schema = tmp_path / "elsewhere" / "_agents" / "mirrors" / "masterplan"
    other_schema.mkdir(parents=True)
    rc, _out, err, calls = _run_enable_script(
        tmp_path, _env(tmp_path, [taskdef, other_schema]),
    )
    assert rc == 0
    assert _marketplace_add_target(calls) == str(other_schema)
    assert "not the canonical (first) candidate" in err
    assert "work-repo" not in err and "deprecated" not in err
