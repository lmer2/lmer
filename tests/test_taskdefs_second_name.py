"""Tests for the taskdef tier search's second name (schema-form project dir).

In a plexus carrying a ``_lmer.yaml`` manifest (spec D7/D8/D20) a project
is a flat top-level dir named by SHORT NAME and its identity is the
registry's ``repo:`` + ``remotes:`` — never the ``{host}/{project}`` path.
``work_repo_taskdef_dirs`` therefore has to resolve the project-scoped tier
by normalized registry lookup first and fall through to the legacy
``{host}/{project}`` dir (the host bucket, D9) when there is no manifest or
no match. A manifest that is present but unusable is FAIL-CLOSED (plan T6,
D20): the project tier resolves to nothing rather than to the legacy dir,
and /start says so. The search lives in two deliberately-mirrored copies
(``lmer_cli.container.taskdefs`` and ``hooks/start.py``, which imports no
project code); both are exercised here and pinned equal by the same AST
body guard ``tests/test_masterplan_provisioning.py`` uses.
"""
import textwrap

import pytest

from hooks import start as start_hook
from lmer_cli.container import taskdefs
from lmer_cli.container.sources import normalize_repo_url
from tests.conftest import ast_body_lines, strip_lmer_env

COPIES = pytest.mark.parametrize("mod", [taskdefs, start_hook], ids=["taskdefs", "start"])

MANIFEST = textwrap.dedent(
    """\
    schema: 1
    name: example
    projects:
      lmer:
        repo: git.example.com/group/proj
        remotes:
          - github.com/example/lmer
      other:
        repo: git.example.com/group/other
      q3-capacity:
        description: no repo at all
    """
)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _work_repo(tmp_path, manifest=MANIFEST):
    work = tmp_path / "work"
    work.mkdir()
    if manifest is not None:
        (work / "_lmer.yaml").write_text(manifest)
    return work


def _legacy_env(monkeypatch, work):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")


def _mk(*parts):
    path = parts[0].joinpath(*parts[1:])
    path.mkdir(parents=True)
    return path


# ── Schema-form resolution ─────────────────────────────────────────────


@COPIES
def test_schema_form_match_by_repo(mod, monkeypatch, tmp_path):
    """The launch's LMER_REPO_URL matches a registry ``repo:`` after
    normalization (scheme, ``.git``, case are all irrelevant)."""
    work = _work_repo(tmp_path)
    _legacy_env(monkeypatch, work)
    monkeypatch.setenv("LMER_REPO_URL", "https://Git.Example.com/group/proj.git")
    schema_dir = _mk(work, "lmer", "_taskdef")
    global_dir = _mk(work, "taskdef")

    assert mod.work_repo_taskdef_dirs() == [schema_dir, global_dir]


@COPIES
def test_schema_form_match_by_remotes_entry(mod, monkeypatch, tmp_path):
    """A ``remotes:`` entry is as good as ``repo:`` — multi-remote projects
    land in ONE tree (D8)."""
    work = _work_repo(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    monkeypatch.setenv("LMER_REPO_HOST", "github.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "example/lmer")
    monkeypatch.setenv("LMER_REPO_URL", "git@github.com:example/lmer.git")
    schema_dir = _mk(work, "lmer", "_taskdef")

    assert mod.work_repo_taskdef_dirs() == [schema_dir]


@COPIES
def test_schema_form_match_by_userless_scp_url(mod, monkeypatch, tmp_path):
    """Git's scp syntax is ``[user@]host:path`` — an ssh_config alias
    checkout reports the userless form, and it names the same repo as
    ``git@host:path`` (wave-1 review finding on the one normalizer)."""
    work = _work_repo(tmp_path)
    _legacy_env(monkeypatch, work)
    monkeypatch.setenv("LMER_REPO_URL", "git.example.com:group/proj.git")
    schema_dir = _mk(work, "lmer", "_taskdef")

    assert mod.work_repo_taskdef_dirs() == [schema_dir]


@COPIES
def test_schema_form_lookup_from_host_and_project(mod, monkeypatch, tmp_path):
    """Without LMER_REPO_URL the lookup key is https://{host}/{project}."""
    work = _work_repo(tmp_path)
    _legacy_env(monkeypatch, work)
    monkeypatch.delenv("LMER_REPO_URL", raising=False)
    schema_dir = _mk(work, "lmer", "_taskdef")

    assert mod.work_repo_taskdef_dirs() == [schema_dir]


@COPIES
def test_schema_form_precedes_legacy_then_global(mod, monkeypatch, tmp_path):
    """Full precedence: schema-form project dir, legacy {host}/{project},
    work-global — every existing tier keeps its slot."""
    work = _work_repo(tmp_path)
    _legacy_env(monkeypatch, work)
    schema_dir = _mk(work, "lmer", "_taskdef")
    legacy_dir = _mk(work, "git.example.com", "group", "proj", "taskdef")
    global_dir = _mk(work, "taskdef")

    assert mod.work_repo_taskdef_dirs() == [schema_dir, legacy_dir, global_dir]


@COPIES
def test_bare_taskdef_name_grandfathered_in_schema_form(mod, monkeypatch, tmp_path):
    """D6: bare legacy names stay recognized until a tree migrates, so a
    named project dir still holding ``taskdef/`` resolves; ``_taskdef/``
    wins when both exist."""
    work = _work_repo(tmp_path)
    _legacy_env(monkeypatch, work)
    bare = _mk(work, "lmer", "taskdef")
    assert mod.work_repo_taskdef_dirs() == [bare]

    underscored = _mk(work, "lmer", "_taskdef")
    assert mod.work_repo_taskdef_dirs() == [underscored, bare]


@COPIES
def test_global_tier_second_name(mod, monkeypatch, tmp_path):
    """The instance-level tier is ``_taskdef/`` in schema form (today
    ``taskdef/``); both names resolve, ``_taskdef/`` first."""
    work = _work_repo(tmp_path, manifest=None)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    bare = _mk(work, "taskdef")
    underscored = _mk(work, "_taskdef")

    assert mod.work_repo_taskdef_dirs() == [underscored, bare]


# ── Fallthrough to the legacy host-bucket path ─────────────────────────


@COPIES
def test_no_manifest_legacy_only(mod, monkeypatch, tmp_path):
    """No ``_lmer.yaml`` = legacy form: the registry is never consulted
    and a short-name dir that happens to exist is NOT picked up."""
    work = _work_repo(tmp_path, manifest=None)
    _legacy_env(monkeypatch, work)
    monkeypatch.setenv("LMER_REPO_URL", "https://git.example.com/group/proj.git")
    _mk(work, "lmer", "_taskdef")
    legacy_dir = _mk(work, "git.example.com", "group", "proj", "taskdef")
    global_dir = _mk(work, "taskdef")

    assert mod.work_repo_taskdef_dirs() == [legacy_dir, global_dir]


@COPIES
def test_manifest_without_match_legacy_only(mod, monkeypatch, tmp_path):
    """D9: an unregistered repo gets no invented name — its work stays in
    the ``{host}/{project}`` host bucket."""
    work = _work_repo(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "group/unnamed")
    monkeypatch.setenv("LMER_REPO_URL", "https://git.example.com/group/unnamed")
    _mk(work, "lmer", "_taskdef")
    legacy_dir = _mk(work, "git.example.com", "group", "unnamed", "taskdef")

    assert mod.work_repo_taskdef_dirs() == [legacy_dir]


@COPIES
def test_non_default_port_is_a_different_repo(mod, monkeypatch, tmp_path):
    """normalize_repo_url keeps a non-default port, so ``host:2222/path``
    does not match a registry entry for ``host/path`` (spec §4)."""
    work = _work_repo(tmp_path)
    _legacy_env(monkeypatch, work)
    monkeypatch.setenv("LMER_REPO_URL", "ssh://git@git.example.com:2222/group/proj")
    _mk(work, "lmer", "_taskdef")
    legacy_dir = _mk(work, "git.example.com", "group", "proj", "taskdef")

    assert mod.work_repo_taskdef_dirs() == [legacy_dir]


UNUSABLE_MANIFESTS = [
    pytest.param("{ not yaml\n", id="not-yaml"),
    pytest.param("- a\n- list\n", id="not-a-mapping"),
    pytest.param("", id="empty"),
    pytest.param("projects: [lmer]\n", id="projects-not-a-mapping"),
]


@COPIES
@pytest.mark.parametrize("body", UNUSABLE_MANIFESTS)
def test_unusable_manifest_fails_closed(mod, monkeypatch, tmp_path, body):
    """A manifest that is present but unusable is NOT demoted to legacy
    (plan T6, D20 — a one-character typo must never point /start at the
    stale pre-migration ``{host}/{project}`` bucket): the project-scoped
    tier resolves to NOTHING, schema or legacy, while the global tier
    still applies. Same contract as hooks/run_state_guard.py's
    ``derive_run_dir``; /start reports it (see the start.py tests below)."""
    work = _work_repo(tmp_path, manifest=body)
    _legacy_env(monkeypatch, work)
    _mk(work, "lmer", "_taskdef")
    _mk(work, "git.example.com", "group", "proj", "taskdef")
    global_dir = _mk(work, "taskdef")

    assert mod.work_repo_taskdef_dirs() == [global_dir]


@COPIES
def test_manifest_that_is_a_directory_fails_closed(mod, monkeypatch, tmp_path):
    """Present-but-unreadable (a directory where the file should be) is
    the same diagnosis as unparseable — run_state_guard agrees."""
    work = _work_repo(tmp_path, manifest=None)
    (work / "_lmer.yaml").mkdir()
    _legacy_env(monkeypatch, work)
    _mk(work, "git.example.com", "group", "proj", "taskdef")
    global_dir = _mk(work, "taskdef")

    assert mod.work_repo_taskdef_dirs() == [global_dir]
    assert mod.plexus_manifest_problem() == "unreadable"


@COPIES
@pytest.mark.parametrize(
    "body",
    [
        pytest.param("schema: 1\nname: example\n", id="no-projects-key"),
        pytest.param("schema: 1\nprojects:\n", id="null-projects"),
        pytest.param("projects:\n  lmer: git.example.com/group/proj\n", id="odd-entry"),
    ],
)
def test_usable_manifest_without_a_match_is_the_host_bucket(mod, monkeypatch, tmp_path, body):
    """Usable but no hit — no ``projects:`` at all (an empty registry), or
    an entry that is not a mapping (skipped, the validator's complaint) —
    is the unregistered case: the host bucket resolves (D9)."""
    work = _work_repo(tmp_path, manifest=body)
    _legacy_env(monkeypatch, work)
    _mk(work, "lmer", "_taskdef")
    legacy_dir = _mk(work, "git.example.com", "group", "proj", "taskdef")

    assert mod.work_repo_taskdef_dirs() == [legacy_dir]
    assert mod.plexus_manifest_problem() is None


@COPIES
def test_plexus_manifest_problem_reports_the_reason(mod, monkeypatch, tmp_path):
    """The library copy exposes WHY the tier was skipped as a return value
    (no manifest / usable manifest → None) so callers can surface it."""
    work = _work_repo(tmp_path, manifest=None)
    _legacy_env(monkeypatch, work)
    assert mod.plexus_manifest_problem() is None

    (work / "_lmer.yaml").write_text(MANIFEST)
    assert mod.plexus_manifest_problem() is None

    for body, reason in [
        ("{ not yaml\n", "not valid YAML"),
        ("- a\n- list\n", "document is not a mapping"),
        ("", "document is not a mapping"),
        ("projects: [lmer]\n", "projects: is not a mapping"),
    ]:
        (work / "_lmer.yaml").write_text(body)
        assert mod.plexus_manifest_problem() == reason, body

    monkeypatch.delenv("LMER_WORK_REPO_PATH")
    assert mod.plexus_manifest_problem() is None


def test_start_warns_when_manifest_is_unusable(monkeypatch, tmp_path):
    """hooks/start.py is not silent about a skipped project tier: one
    greppable line through the same diagnostics slot as TASKDEF SHADOW."""
    work = _work_repo(tmp_path, manifest="projects: [lmer]\n")
    _legacy_env(monkeypatch, work)

    assert start_hook.plexus_manifest_warning_lines() == [
        f"⚠️  PLEXUS MANIFEST: {work / '_lmer.yaml'} present but unusable "
        "(projects: is not a mapping); project taskdef tier skipped — fix _lmer.yaml"
    ]

    (work / "_lmer.yaml").write_text(MANIFEST)
    assert start_hook.plexus_manifest_warning_lines() == []


def test_start_prints_manifest_diagnostic_under_location(monkeypatch, tmp_path, capsys):
    """End to end through read_and_display_instructions(): the line sits
    after 📍 Location and before the separator, and /start still renders
    (the global tier is unaffected)."""
    work = _work_repo(tmp_path, manifest="{ not yaml\n")
    _legacy_env(monkeypatch, work)
    global_dir = _mk(work, "taskdef", "develop")
    instructions = global_dir / "instructions.txt"
    instructions.write_text("hello from the global tier\n")
    monkeypatch.setattr(start_hook, "run_state_session_start", lambda: "")
    monkeypatch.setattr(start_hook.Path, "home", lambda: tmp_path)

    assert start_hook.read_and_display_instructions(instructions) is True
    out = capsys.readouterr().out
    (line,) = [l for l in out.splitlines() if "PLEXUS MANIFEST" in l]
    assert "present but unusable (not valid YAML)" in line
    assert "project taskdef tier skipped" in line
    assert out.index("📍 Location:") < out.index("PLEXUS MANIFEST") < out.index("=" * 60)
    assert "hello from the global tier" in out


@COPIES
def test_invalid_short_name_is_ignored(mod, monkeypatch, tmp_path):
    """The work repo is agent-writable: a registry key that is not a §1
    short name (``[a-z0-9][a-z0-9_-]*``) must never become a path segment."""
    manifest = textwrap.dedent(
        """\
        schema: 1
        projects:
          ../escape:
            repo: git.example.com/group/proj
        """
    )
    work = _work_repo(tmp_path, manifest=manifest)
    _legacy_env(monkeypatch, work)
    _mk(tmp_path, "escape", "_taskdef")
    legacy_dir = _mk(work, "git.example.com", "group", "proj", "taskdef")

    assert mod.work_repo_taskdef_dirs() == [legacy_dir]


@COPIES
def test_find_taskdef_file_resolves_through_schema_dir(mod, monkeypatch, tmp_path):
    """End to end: the schema-form project tier shadows the global tier."""
    work = _work_repo(tmp_path)
    _legacy_env(monkeypatch, work)
    monkeypatch.setenv("LMER_TASK", "develop")
    schema_dir = _mk(work, "lmer", "_taskdef", "develop")
    (schema_dir / "instructions.txt").write_text("schema\n")
    global_dir = _mk(work, "taskdef", "develop")
    (global_dir / "instructions.txt").write_text("global\n")

    assert mod.find_taskdef_file("instructions.txt") == schema_dir / "instructions.txt"


def test_shadow_label_names_schema_form_project_tier(monkeypatch, tmp_path):
    """hooks/start.py's shadow diagnostics label the schema-form project dir
    as the project tier, same as the legacy dir."""
    work = _work_repo(tmp_path)
    _legacy_env(monkeypatch, work)
    schema_dir = _mk(work, "lmer", "_taskdef")
    legacy_dir = _mk(work, "git.example.com", "group", "proj", "taskdef")
    global_dir = _mk(work, "_taskdef")

    assert start_hook._shadow_tier_label(schema_dir) == "work-repo project tier"
    assert start_hook._shadow_tier_label(legacy_dir) == "work-repo project tier"
    assert start_hook._shadow_tier_label(global_dir) == "work-repo global tier"
    assert start_hook._shadow_tier_label(tmp_path / "ext").startswith(
        "LMER_TASKDEF_PATHS entry "
    )


# ── Mirror guards ───────────────────────────────────────────────────────


def test_both_copies_still_equal():
    """The two copies stay byte-for-byte in body, including the new
    registry-lookup helpers (same guard as test_masterplan_provisioning)."""
    for name in (
        "_userinfo_boundary",
        "_normalize_repo_url",
        "_plexus_project_dir",
        "plexus_manifest_problem",
        "work_repo_taskdef_dirs",
        "builtin_taskdef_root",
        "taskdef_search_dirs",
        "find_taskdef_file",
    ):
        assert ast_body_lines(getattr(taskdefs, name)) == ast_body_lines(
            getattr(start_hook, name)
        ), f"{name} body diverged between lmer_cli.container.taskdefs and hooks/start.py"


@pytest.mark.parametrize(
    "url",
    [
        "https://git.example.com/group/proj.git",
        "HTTPS://Git.Example.com:443/group/Proj/",
        "git@git.example.com:group/proj.git",
        "ssh://git@git.example.com/group/proj",
        "ssh://git@git.example.com:22/group/proj",
        "ssh://git@git.example.com:2222/group/proj",
        "https://user:tok@git.example.com/group/proj",
        "https://user:se/cret@git.example.com/group/proj.git",
        "https://git.example.com:8443/group/proj",
        "git.example.com/group/proj",
        "git.example.com:group/re@po.git",
        # Userless scp form and the scheme-less shapes kept out of it (a
        # bare-digit segment after the colon is a port, not a path).
        "git.example.com:group/proj.git",
        "Git.Example.COM:group/proj",
        "git.example.com:/srv/git/proj.git",
        "git.example.com:2222/group/proj",
        "git.example.com:2222",
        "host:8080/org/repo@v1.git",
        "git@git.example.com:2024/reports",
        "git.example.com:2024/reports",
        "srv/git:proj",
        "",
        None,
    ],
)
def test_inline_normalizer_matches_canonical(url):
    """D21: remote normalization has ONE definition. The hook cannot import
    it, so its inline mirror is pinned to normalize_repo_url behaviorally."""
    assert taskdefs._normalize_repo_url(url) == normalize_repo_url(url)
    assert start_hook._normalize_repo_url(url) == normalize_repo_url(url)


def test_inline_userinfo_boundary_matches_canonical():
    from lmer_cli.container import sources

    assert ast_body_lines(taskdefs._userinfo_boundary) == ast_body_lines(
        sources._userinfo_boundary
    )
