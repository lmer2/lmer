"""Second-name (schema-form) run-dir resolution in hooks/run_state_guard.py.

In a plexus with a ``_plexus.yaml`` manifest the project is a flat short-name
dir resolved from the manifest's registry (spec §4, D7/D8/D20) and runs live at
``{work}/{shortname}/_runs/{slug}/`` (D22). The hook must find that dir — and
keep finding the legacy ``{host}/{project}/runs/{slug}`` one — without
importing any project code, so the registry lookup and the remote
normalization are spelled out inline and pinned here against the one
definition in ``lmer_cli.container.sources``.
"""
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from hooks.run_state_guard import (
    COUNTER_TEMPLATE,
    LEDGER_SENTINEL_TEMPLATE,
    PLEXUS_MANIFEST,
    SENTINEL_TEMPLATE,
    derive_run_dir,
    normalize_repo_url,
    read_plexus_registry,
    registry_lookup,
)

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "run_state_guard.py"

HOST = "git.example.com"
PROJECT = "group/project"
SLUG = "develop-run-naming"
SHORT = "proj"

# Registry fixture: one repo-backed entry with a primary and two extra
# remotes, one single-remote entry, one repoless entry (D19), one null entry.
MANIFEST = f"""\
schema: 1
name: example
projects:
  {SHORT}:
    repo: {HOST}/{PROJECT}
    remotes:
      - github.com/Example/Project
      - ssh://git@{HOST}:2222/{PROJECT}
  other:
    repo: {HOST}/other/repo
  research:
    description: a repoless workstream
  empty:
sources: {{}}
"""


def _decision(slug=SLUG, **extra):
    decision = {"kind": "run", "slug": slug, "phase": "develop",
                "goal": "ship it", "name": "run-naming"}
    decision.update(extra)
    return decision


def _plexus(work, text=MANIFEST):
    work.mkdir(parents=True, exist_ok=True)
    (work / PLEXUS_MANIFEST).write_text(text, encoding="utf-8")
    return work


# ---- derive_run_dir: schema form ----------------------------------------------------

class TestSchemaFormResolution:
    def test_explicit_run_dir_still_wins(self, tmp_path):
        work = _plexus(tmp_path / "work")
        decision = _decision(run_dir="/elsewhere/_runs/foo/sess")
        assert derive_run_dir(decision, HOST, PROJECT, str(work)) == "/elsewhere/_runs/foo/sess"

    def test_match_by_repo_from_host_and_project(self, tmp_path):
        work = _plexus(tmp_path / "work")
        assert derive_run_dir(_decision(), HOST, PROJECT, str(work)) == \
            os.path.join(str(work), SHORT, "_runs", SLUG)

    def test_match_by_repo_url_env_value(self, tmp_path):
        work = _plexus(tmp_path / "work")
        url = f"https://oauth2:token@{HOST}/{PROJECT}.git"
        assert derive_run_dir(_decision(), HOST, PROJECT, str(work), repo_url=url) == \
            os.path.join(str(work), SHORT, "_runs", SLUG)

    @pytest.mark.parametrize("url", [
        "git@github.com:example/project.git",
        "github.com:example/project.git",
        "https://github.com/example/project",
        f"ssh://git@{HOST}:2222/{PROJECT}",
    ])
    def test_match_by_remotes_entry(self, tmp_path, url):
        work = _plexus(tmp_path / "work")
        # host/project name a repo the registry does NOT list; the launch URL
        # (LMER_REPO_URL) is what is looked up, across repo: and remotes:.
        assert derive_run_dir(_decision(), "unrelated.example", "x/y", str(work),
                              repo_url=url) == \
            os.path.join(str(work), SHORT, "_runs", SLUG)

    def test_repo_url_wins_over_host_project_for_lookup(self, tmp_path):
        work = _plexus(tmp_path / "work")
        assert derive_run_dir(_decision(), HOST, PROJECT, str(work),
                              repo_url=f"https://{HOST}/other/repo") == \
            os.path.join(str(work), "other", "_runs", SLUG)

    @pytest.mark.parametrize("slug", [None, "", "   "])
    def test_missing_slug_falls_back_to_runs_base(self, tmp_path, slug):
        work = _plexus(tmp_path / "work")
        assert derive_run_dir(_decision(slug=slug), HOST, PROJECT, str(work)) == \
            os.path.join(str(work), SHORT, "_runs")

    def test_non_dict_decision_falls_back_to_runs_base(self, tmp_path):
        work = _plexus(tmp_path / "work")
        assert derive_run_dir(None, HOST, PROJECT, str(work)) == \
            os.path.join(str(work), SHORT, "_runs")

    @pytest.mark.parametrize("host,project", [
        (None, PROJECT), ("", PROJECT), (HOST, None), (HOST, ""),
    ])
    def test_no_run_context_returns_none_even_with_manifest(self, tmp_path, host, project):
        work = _plexus(tmp_path / "work")
        assert derive_run_dir(_decision(), host, project, str(work)) is None


# ---- derive_run_dir: legacy fallback and fail-open ----------------------------------

class TestLegacyFallback:
    def test_no_manifest_is_legacy(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        assert derive_run_dir(_decision(), HOST, PROJECT, str(work)) == \
            os.path.join(str(work), HOST, PROJECT, "runs", SLUG)

    def test_missing_work_repo_is_legacy(self, tmp_path):
        work = tmp_path / "nowhere"
        assert derive_run_dir(_decision(), HOST, PROJECT, str(work)) == \
            os.path.join(str(work), HOST, PROJECT, "runs", SLUG)

    def test_no_registry_match_is_legacy_host_bucket(self, tmp_path):
        # D9: an unregistered repo is never named; its work sits in the
        # {host}/{project} host bucket, which is the legacy path.
        work = _plexus(tmp_path / "work")
        assert derive_run_dir(_decision(), "unknown.example", "a/b", str(work)) == \
            os.path.join(str(work), "unknown.example", "a/b", "runs", SLUG)

    def test_empty_registry_is_legacy(self, tmp_path):
        work = _plexus(tmp_path / "work", "schema: 1\nname: example\n")
        assert derive_run_dir(_decision(), HOST, PROJECT, str(work)) == \
            os.path.join(str(work), HOST, PROJECT, "runs", SLUG)

    @pytest.mark.parametrize("text", [
        "schema: 1\nprojects: [not: a: mapping\n",     # YAML syntax error
        "- just\n- a list\n",                           # document not a mapping
        "schema: 1\nprojects:\n  - not\n  - a mapping\n",  # registry not a mapping
    ])
    def test_unparseable_manifest_returns_none(self, tmp_path, text):
        # D20: a present-but-broken manifest is NOT demoted to legacy form —
        # that would point trigger 2 at {host}/{project}/runs/ inside a
        # schema-form tree. The hook has no base to resolve, so it returns
        # None and blocks nothing (fail open), the same posture the work
        # CLI's hook-facing verbs take.
        work = _plexus(tmp_path / "work", text)
        assert derive_run_dir(_decision(), HOST, PROJECT, str(work)) is None

    def test_unreadable_manifest_returns_none(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        (work / PLEXUS_MANIFEST).mkdir()  # a directory where the file should be
        assert derive_run_dir(_decision(), HOST, PROJECT, str(work)) is None


# ---- read_plexus_registry / registry_lookup ----------------------------------------

class TestRegistryHelpers:
    def test_read_registry_absent(self, tmp_path):
        assert read_plexus_registry(str(tmp_path)) is None

    def test_read_registry_parsed(self, tmp_path):
        _plexus(tmp_path)
        registry = read_plexus_registry(str(tmp_path))
        assert set(registry) == {SHORT, "other", "research", "empty"}

    def test_read_registry_without_projects_key_is_empty(self, tmp_path):
        _plexus(tmp_path, "schema: 1\n")
        assert read_plexus_registry(str(tmp_path)) == {}

    def test_lookup_matches_repo_and_remotes(self):
        registry = yaml.safe_load(MANIFEST)["projects"]
        assert registry_lookup(registry, f"https://{HOST}/{PROJECT}.git") == SHORT
        assert registry_lookup(registry, "git@github.com:Example/Project.git") == SHORT
        # Userless scp form (git's `[user@]host:path`, the user optional).
        assert registry_lookup(registry, "github.com:Example/Project.git") == SHORT
        assert registry_lookup(registry, f"{HOST}:{PROJECT}") == SHORT
        assert registry_lookup(registry, f"https://{HOST}/other/repo") == "other"

    def test_lookup_path_is_case_insensitive(self):
        registry = {"p": {"repo": "github.com/Example/Project"}}
        assert registry_lookup(registry, "https://github.com/example/PROJECT") == "p"

    def test_lookup_port_is_significant(self):
        registry = {"p": {"repo": f"{HOST}/{PROJECT}"}}
        assert registry_lookup(registry, f"ssh://git@{HOST}:2222/{PROJECT}") is None
        assert registry_lookup(registry, f"ssh://git@{HOST}:22/{PROJECT}") == "p"

    @pytest.mark.parametrize("registry", [
        {"p": None},
        {"p": "git.example.com/x"},
        {"p": {"repo": None, "remotes": None}},
        {"p": {"repo": 42, "remotes": "not-a-list"}},
        {"p": {"remotes": [None, 7, ""]}},
        {"p": {"description": "repoless"}},
    ])
    def test_lookup_tolerates_odd_entries(self, registry):
        assert registry_lookup(registry, f"https://{HOST}/{PROJECT}") is None

    def test_lookup_ignores_blank_url(self):
        registry = {"p": {"repo": ""}}
        assert registry_lookup(registry, "") is None
        assert registry_lookup(registry, None) is None

    @pytest.mark.parametrize("key", ["../x", "/abs", "Lmer"])
    def test_lookup_rejects_keys_that_are_not_short_names(self, key):
        # The work repo is agent-writable: a registry key that is not a §1
        # short name ([a-z0-9][a-z0-9_-]*) must never become a path segment
        # — the same check taskdefs._plexus_project_dir applies.
        registry = {key: {"repo": f"{HOST}/{PROJECT}"}}
        assert registry_lookup(registry, f"https://{HOST}/{PROJECT}") is None

    def test_lookup_accepts_the_short_name_charset(self):
        registry = {"my_proj-2": {"repo": f"{HOST}/{PROJECT}"}}
        assert registry_lookup(registry, f"https://{HOST}/{PROJECT}") == "my_proj-2"

    @pytest.mark.parametrize("key", ["../x", "/abs", "Lmer"])
    def test_derive_run_dir_never_leaves_the_work_root(self, tmp_path, key):
        # A hostile key does not resolve, so the repo is unnamed and lands in
        # the host bucket — inside the work root, never ../ or an absolute
        # path (os.path.join resets on an absolute component).
        work = _plexus(tmp_path / "work",
                       f"schema: 1\nprojects:\n  {key}:\n    repo: {HOST}/{PROJECT}\n")
        result = derive_run_dir(_decision(), HOST, PROJECT, str(work))
        assert result == os.path.join(str(work), HOST, PROJECT, "runs", SLUG)
        assert os.path.commonpath([str(work), os.path.normpath(result)]) == str(work)


# ---- normalize_repo_url: inline replica pinned to the one definition (D21) ----------

NORMALIZE_CASES = [
    f"https://{HOST}/{PROJECT}.git",
    f"git@{HOST}:{PROJECT}",
    f"ssh://git@{HOST}/{PROJECT}.git",
    f"ssh://git@{HOST}:2222/{PROJECT}",
    "https://GIT.EXAMPLE.COM:443/Group/Project/",
    f"https://oauth2:glpat-secret@{HOST}/{PROJECT}.git",
    f"{HOST}/{PROJECT}",
    "https://github.com/example/lmer",
    "host:8080/org/repo@v1.git",
    # Userless scp form and the scheme-less shapes kept out of it (a
    # bare-digit segment after the colon is a port, not a path).
    f"{HOST}:{PROJECT}.git",
    f"{HOST.upper()}:{PROJECT}",
    f"{HOST}:/{PROJECT}.git",
    f"{HOST}:2222/{PROJECT}",
    f"{HOST}:2222",
    f"git@{HOST}:2024/reports",
    f"{HOST}:2024/reports",
    f"{HOST}:org/re@po.git",
    "srv/git:repo",
    "",
    "   ",
]


class TestNormalizeReplica:
    def test_spec_examples(self):
        # The spec's examples with its placeholders swapped for this file's;
        # every rule they exercise (scheme + .git strip, scp form, a
        # non-default port kept) is unchanged.
        assert normalize_repo_url(f"https://{HOST}/{PROJECT}.git") == \
            f"{HOST}/{PROJECT}"
        assert normalize_repo_url(f"git@{HOST}:{PROJECT}") == \
            f"{HOST}/{PROJECT}"
        assert normalize_repo_url(f"{HOST}:{PROJECT}.git") == \
            f"{HOST}/{PROJECT}"
        assert normalize_repo_url(f"ssh://git@{HOST}:2222/{PROJECT}") == \
            f"{HOST}:2222/{PROJECT}"
        assert normalize_repo_url("https://GIT.EXAMPLE.COM:443/Group/Project/") == \
            f"{HOST}/Group/Project"
        assert normalize_repo_url(f"https://oauth2:glpat-secret@{HOST}/{PROJECT}.git") == \
            f"{HOST}/{PROJECT}"

    @pytest.mark.parametrize("url", NORMALIZE_CASES)
    def test_matches_sources_normalize_repo_url(self, url):
        # Drift guard: the hook may not import lmer_cli, so the replica is
        # pinned against the real thing from the test side.
        from lmer_cli.container.sources import normalize_repo_url as canonical
        assert normalize_repo_url(url) == canonical(url)


# ---- standing rule: the hook imports no project code -------------------------------

class TestNoProjectImports:
    def test_hook_imports_no_project_code(self):
        pattern = re.compile(
            r"^[ \t]*(from|import)[ \t]+(lmer_cli|work_repo|lmer_platform|lmer_schema)",
            re.MULTILINE,
        )
        assert pattern.search(HOOK.read_text(encoding="utf-8")) is None


# ---- end to end: main() finds the schema-form run dir -----------------------------

GUARD_ENV_VARS = (
    "LMER_RUN_STATE_GUARD",
    "LMER_REPO_HOST",
    "LMER_REPO_PROJECT",
    "LMER_REPO_URL",
    "LMER_SESSION_ID",
    "LMER_WORK_REPO_PATH",
)


@pytest.fixture
def session():
    sid = f"pytest-{uuid.uuid4().hex}"
    yield sid
    for template in (SENTINEL_TEMPLATE, COUNTER_TEMPLATE, LEDGER_SENTINEL_TEMPLATE):
        try:
            os.unlink(template.format(session=sid))
        except OSError:
            pass


def _run_hook(payload, env_extra):
    env = os.environ.copy()
    for var in GUARD_ENV_VARS:
        env.pop(var, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd),
         "-c", "user.name=test", "-c", "user.email=test@example.com",
         *args],
        check=True, capture_output=True, text=True,
    )


def _make_work_cli(tmp_path, decision):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "work"
    script.write_text(
        f"#!/bin/sh\ncat <<'WORK_EOF'\n{json.dumps(decision)}\nWORK_EOF\nexit 0\n")
    script.chmod(0o755)
    return bin_dir


def _make_schema_work_repo(tmp_path):
    """Schema-form work-repo clone: manifest + a pushed _runs/<slug>/<session>/."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True, text=True)
    clone = tmp_path / "workrepo"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   check=True, capture_output=True, text=True)
    _plexus(clone)
    run_dir = clone / SHORT / "_runs" / SLUG
    session_dir = run_dir / "sess-0001"
    session_dir.mkdir(parents=True)
    (session_dir / "notes.md").write_text("artifact\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "run dir")
    _git(clone, "push", "-q", "-u", "origin", "main")
    return clone, run_dir


def _env(tmp_path, session, work_repo, repo_url=None):
    env = {
        "PATH": f"{_make_work_cli(tmp_path, _decision())}{os.pathsep}{os.environ.get('PATH', '')}",
        "LMER_REPO_HOST": HOST,
        "LMER_REPO_PROJECT": PROJECT,
        "LMER_SESSION_ID": session,
        "LMER_WORK_REPO_PATH": str(work_repo),
    }
    if repo_url is not None:
        env["LMER_REPO_URL"] = repo_url
    return env


class TestMainSchemaForm:
    def test_dirty_session_dir_blocks_with_schema_path(self, tmp_path, session):
        work_repo, run_dir = _make_schema_work_repo(tmp_path)
        (run_dir / "sess-0001" / "scratch.md").write_text("uncommitted\n")
        r = _run_hook({"cwd": str(tmp_path)}, _env(tmp_path, session, work_repo))
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert out["decision"] == "block"
        assert str(run_dir) in out["reason"]
        assert "uncommitted changes" in out["reason"]

    def test_match_via_repo_url_env(self, tmp_path, session):
        work_repo, run_dir = _make_schema_work_repo(tmp_path)
        (run_dir / "sess-0001" / "scratch.md").write_text("uncommitted\n")
        env = _env(tmp_path, session, work_repo, repo_url="git@github.com:example/project.git")
        env["LMER_REPO_HOST"] = "unrelated.example"
        env["LMER_REPO_PROJECT"] = "x/y"
        out = json.loads(_run_hook({"cwd": str(tmp_path)}, env).stdout)
        assert out["decision"] == "block"
        assert str(run_dir) in out["reason"]

    def test_dirt_in_legacy_path_does_not_block_in_schema_form(self, tmp_path, session):
        work_repo, _ = _make_schema_work_repo(tmp_path)
        legacy = work_repo / HOST / PROJECT / "runs" / SLUG
        legacy.mkdir(parents=True)
        (legacy / "scratch.md").write_text("elsewhere\n")
        r = _run_hook({"cwd": str(tmp_path)}, _env(tmp_path, session, work_repo))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_broken_manifest_fails_open(self, tmp_path, session):
        work_repo, run_dir = _make_schema_work_repo(tmp_path)
        (work_repo / PLEXUS_MANIFEST).write_text("projects: [broken\n")
        (run_dir / "sess-0001" / "scratch.md").write_text("uncommitted\n")
        r = _run_hook({"cwd": str(tmp_path)}, _env(tmp_path, session, work_repo))
        assert r.returncode == 0
        assert r.stdout.strip() == ""
