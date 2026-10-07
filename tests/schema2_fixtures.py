"""Shared schema-2 work-repo tree builder for the project-index tests.

The project index renders what a work repo's run records say, so its tests
need many records of controlled shape: named and unnamed, signed out and
not, quoted and unquoted stamps, legacy and opaque ids, flat and grouped,
readable and corrupt, and continuation chains whose `context:` blocks nest
the whole source record the way `run_state._source_context` nests it. This
module is the one place those shapes are spelled out:

- :class:`RecordSpec` describes one record; :func:`build_schema2_plexus`
  writes a `schema: 2` manifest plus the records of each project (the
  `tests/test_schema_v2_sessions.py` hand-written recipe) and returns a
  :class:`Schema2Tree` address book. :meth:`Schema2Tree.add` writes more
  records into a built tree, so a test can add a sibling and re-render.
- :func:`chain` writes a continuation chain: each record's `context` nests
  its source's parsed state plus the source's last :data:`CONTEXT_EVENTS`
  events, so a 20-deep chain grows exactly as a live one does
  (`run_state.py` `_source_context` / `seed_run_dir`); an override may be a
  callable of the level, so shapes can vary along the chain.
- :func:`make_env` points the `work` CLI at a built tree, mirroring the
  `session_tree` fixture (`tests/test_schema_v2_sessions.py`).
- :func:`publication2` adds real git around a built tree: a bare origin, the
  primary checkout and peer clones, modelled on the push-safety `publication`
  fixture (`tests/test_work_repo_push_safety.py`) and `claim_clones`
  (`tests/test_schema_v2_release_claims.py`). :func:`isolate_git` is the
  identity and config isolation it applies, on its own for a test that needs
  one plain checkout rather than a publication.
- The helpers the publication and conflict suites share live here too, so
  neither test module reaches into the other: :func:`start_session` and its
  identity helpers, :func:`memory_env`, :func:`state_args`, :func:`row_ids`,
  :func:`rebase_state` and the :func:`record_git_commands` shim.

Every readable record is written with the production emitter call —
``yaml.safe_dump(state, sort_keys=False)``, the line ``run_state.write_state``
writes — over a document in production key order, so the bytes under test are
a live record's: indentless sequences, ``\\x``/``\\u`` escapes for non-ASCII
text, width-80 folds of long and multi-line scalars. The one edit after the
emitter is ``quoted_stamps=False``, which unquotes the stamps.

`tests/plexus_fixtures.py` is left alone: that tree pins the schema-1 shape
for the validator; this one exists for schema-2 records in quantity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from lmer_cli import gate_lock
from tests import timing_budgets as timing
from tests.conftest import strip_lmer_env

HOST = "git.example.test"
GROUP = "acme"
INSTANCE_NAME = "example"
DEFAULT_PROJECT = "widgets"
DEFAULT_SLUG = "chat"
MANIFEST_NAME = "_plexus.yaml"
STATE_FILE = "state.yaml"
EVENTS_FILE = "events.jsonl"
LOG_FILE = "log.yaml"

#: Events a continuation embeds from its source (``_source_context``: ``events[-25:]``).
CONTEXT_EVENTS = 25

#: Filler events a chain level carries so each embedded ``events`` list is full.
_CHAIN_FILLER_EVENTS = CONTEXT_EVENTS - 2

_STAMP_KEYS = ("created", "updated", "started_at", "timestamp", "ts")
_QUOTED_STAMP_RE = re.compile(
    rf"^(?P<head>\s*(?:- )?(?:{'|'.join(_STAMP_KEYS)}): )'(?P<stamp>[^']+)'$", re.M
)


@dataclass(frozen=True)
class RecordSpec:
    """One run record to write.

    ``uid`` is the record's session id (a 32-hex opaque uid for schema 2, a
    legacy ``YYYYMMDD-HHMMSS-n-pid`` id for schema 1) and names its directory
    unless ``flat`` puts the record at the group's own address. ``signed_out``
    decides whether the event log ends in ``session_end`` (``True``), stops at
    ``session_start`` (``False``) or is absent altogether (``None``).
    ``source_uid`` makes the record a continuation of an earlier record of the
    same project (its full state is nested under ``context.state``);
    ``source_path`` overrides the recorded ``context.source_path``.
    ``quoted_stamps=False`` writes every stamp unquoted, so YAML resolves it to
    a ``datetime`` — the hand-edited shape the renderer must coerce.
    """

    uid: str
    slug: str
    started_at: str
    updated: str
    status: str = "in-progress"
    stop_reason: str | None = None
    name: str | None = None
    goal: str | None = None
    log_message: str | None = None
    signed_out: bool | None = True
    source_uid: str | None = None
    phase: str | None = None
    readable: bool = True
    schema: int = 2
    quoted_stamps: bool = True
    flat: bool = False
    source_path: str | None = None
    dir_suffix: str | None = None
    filler_events: int = 0
    taskdef: str | None = None

    @property
    def dir_name(self) -> str:
        """The record directory's name (the group's for a flat record)."""
        if self.flat:
            return self.slug
        return f"{self.uid}--{self.dir_suffix}" if self.dir_suffix else self.uid

    @property
    def dir_rel(self) -> str:
        """Project-relative POSIX path of the record directory."""
        return f"_runs/{self.slug}" if self.flat else f"_runs/{self.slug}/{self.dir_name}"


@dataclass
class _Written:
    """One written record as its continuations embed it."""

    document: dict
    text: str
    events: list[dict]


@dataclass
class Schema2Tree:
    """Where everything is in one built tree, plus the writer for more records."""

    root: Path
    manifest: Path
    repos: dict[str, str | None]
    projects: dict[str, Path]
    records: dict[str, dict[str, Path]] = field(default_factory=dict)
    specs: dict[str, list[RecordSpec]] = field(default_factory=dict)
    written: dict[tuple[str, str], _Written] = field(default_factory=dict, repr=False)

    def project_dir(self, project: str = DEFAULT_PROJECT) -> Path:
        """The project's directory (``<root>/<project>``)."""
        return self.projects[project]

    def readme(self, project: str = DEFAULT_PROJECT) -> Path:
        """The project README the index block lives in."""
        return self.projects[project] / "README.md"

    def record_dir(self, project: str, uid: str) -> Path:
        """A written record's directory, keyed by its uid (slug for a flat record)."""
        return self.records[project][uid]

    def add(self, project: str, *specs: RecordSpec) -> list[Path]:
        """Write more records into *project* and return their directories."""
        return write_records(self, project, specs)

    def chain(self, project: str, depth: int, **overrides) -> list[RecordSpec]:
        """Write a continuation chain into *project*; see :func:`chain`."""
        return chain(self, project, depth, **overrides)


def uid_for(index: int, salt: str = "a") -> str:
    """A deterministic 32-hex uid: ``index`` and ``salt`` make it unique and readable."""
    digest = hashlib.sha256(f"{salt}:{index}".encode()).hexdigest()
    return digest[:32]


def stamp(day: int, hour: int = 10, minute: int = 0, second: int = 0,
          *, month: int = 9, year: int = 2026) -> str:
    """A ``…Z`` ISO stamp on a fixed calendar, for records that must sort."""
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


def manifest_text(repos: Mapping[str, str | None],
                  descriptions: Mapping[str, str] | None = None) -> str:
    """A ``schema: 2`` manifest registering *repos* (``None`` → a repoless project)."""
    lines = ["schema: 2", f"name: {INSTANCE_NAME}", "projects:"]
    for project, repo in repos.items():
        lines.append(f"  {project}:")
        if repo is not None:
            lines.append(f"    repo: {repo}")
        description = (descriptions or {}).get(project)
        if description is not None:
            lines.append(f"    description: {json.dumps(description)}")
    return "\n".join(lines) + "\n"


def build_schema2_plexus(
    root,
    projects: Mapping[str, Sequence[RecordSpec]] | None = None,
    *,
    repos: Mapping[str, str | None] | None = None,
    descriptions: Mapping[str, str] | None = None,
) -> Schema2Tree:
    """Write a schema-2 tree at *root*: manifest, project dirs, records.

    Every project named in *projects* or *repos* is registered and gets a
    directory with an empty ``_runs/``; a project absent from *repos* is
    registered against ``git.example.test/acme/<project>``.
    """
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"fixture root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    projects = dict(projects or {DEFAULT_PROJECT: []})
    registry: dict[str, str | None] = {}
    for project in [*projects, *(repos or {})]:
        if project not in registry:
            registry[project] = (repos or {}).get(project, f"{HOST}/{GROUP}/{project}")
    manifest = root / MANIFEST_NAME
    manifest.write_text(manifest_text(registry, descriptions), encoding="utf-8")
    tree = Schema2Tree(
        root=root, manifest=manifest, repos=registry,
        projects={project: root / project for project in registry},
    )
    for project in registry:
        (root / project / "_runs").mkdir(parents=True)
    for project, specs in projects.items():
        write_records(tree, project, specs)
    return tree


def write_records(tree: Schema2Tree, project: str, specs: Sequence[RecordSpec]) -> list[Path]:
    """Write *specs* into *project* of *tree*, in order, and return their dirs."""
    project_dir = tree.projects[project]
    written_dirs = []
    for spec in specs:
        rdir = project_dir / spec.dir_rel
        rdir.mkdir(parents=True, exist_ok=True)
        events = _events_for(spec)
        document = _state_document(tree, project, spec)
        state_text = _state_text(document, spec)
        if spec.readable:
            (rdir / STATE_FILE).write_text(state_text, encoding="utf-8")
        else:
            (rdir / STATE_FILE).write_text("state: [unclosed\n", encoding="utf-8")
        if events is not None:
            (rdir / EVENTS_FILE).write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
                encoding="utf-8",
            )
        if spec.log_message is not None:
            (rdir / LOG_FILE).write_text(
                yaml.safe_dump(
                    [{"timestamp": spec.started_at, "message": spec.log_message}],
                    sort_keys=False, allow_unicode=True,
                ),
                encoding="utf-8",
            )
        key = spec.slug if spec.flat else spec.uid
        tree.records.setdefault(project, {})[key] = rdir
        tree.specs.setdefault(project, []).append(spec)
        tree.written[(project, spec.uid)] = _Written(document, state_text, events or [])
        written_dirs.append(rdir)
    return written_dirs


def chain(tree: Schema2Tree, project: str, depth: int, *, slug: str = DEFAULT_SLUG,
          salt: str = "chain", start_day: int = 1, **overrides) -> list[RecordSpec]:
    """Write a *depth*-long continuation chain and return its specs, oldest first.

    Level ``k`` continues level ``k-1``: its ``context`` nests the source's
    parsed state and the source's last :data:`CONTEXT_EVENTS` events, and
    every level carries :data:`CONTEXT_EVENTS` events of its own, so the
    deepest record is the largest — the shape a live chain has. An override
    may be a callable taking the level, so a shape can vary along the chain.
    """
    specs: list[RecordSpec] = []
    previous: str | None = None
    for level in range(depth):
        uid = uid_for(level, salt)
        fields = {"goal": f"chain {salt} level {level}", "filler_events": _CHAIN_FILLER_EVENTS}
        fields.update((key, value(level) if callable(value) else value)
                      for key, value in overrides.items())
        spec = RecordSpec(
            uid=uid, slug=slug,
            started_at=stamp(start_day + level // 24, level % 24, 0),
            updated=stamp(start_day + level // 24, level % 24, 5),
            source_uid=previous, **fields,
        )
        write_records(tree, project, [spec])
        specs.append(spec)
        previous = uid
    return specs


def make_env(monkeypatch, root, project: str = DEFAULT_PROJECT, *,
             task: str = "chat", target: str = "") -> Path:
    """Point the `work` CLI at *root* and *project*, as a container launch would.

    Mirrors ``session_tree``: every ``LMER_*`` variable is stripped first, the
    repo host and project come from the project's manifest entry, and the
    one-shot manifest-problem announcement cache is cleared. The gate
    in-flight guard (``gate_lock.GUARD_ENV``) is the one ``LMER_*`` value the
    strip hands back: :func:`publication2` disables it for the test, and a
    `work` push the test drives afterwards must not defer behind a live gate.
    """
    from work_repo import utils

    root = Path(root)
    guard = os.environ.get(gate_lock.GUARD_ENV)
    strip_lmer_env(monkeypatch)
    if guard is not None:
        monkeypatch.setenv(gate_lock.GUARD_ENV, guard)
    document = yaml.safe_load((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    entry = document["projects"][project] or {}
    repo = entry.get("repo") or f"{HOST}/{GROUP}/{project}"
    host, _, repo_project = repo.partition("/")
    for key, value in {
        "LMER_WORK_REPO_PATH": str(root),
        "LMER_REPO_HOST": host,
        "LMER_REPO_PROJECT": repo_project,
        "LMER_REPO_URL": f"https://{repo}",
        "LMER_TASK": task,
        "LMER_TASK_TARGET": target,
    }.items():
        monkeypatch.setenv(key, value)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    return root


def git(repo, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in *repo* with the suite's CLI budget; assert success when *check*."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timing.CLI,
    )
    if check:
        assert result.returncode == 0, f"git {' '.join(args)}: {result.stdout}{result.stderr}"
    return result


@dataclass
class Publication2:
    """A built schema-2 tree under real git: bare origin, primary checkout, peers."""

    origin: Path
    root: Path
    clones: tuple[Path, ...]
    tree: Schema2Tree

    def git(self, repo, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run git in one of this publication's repositories."""
        return git(repo, *args, check=check)

    def origin_file(self, rel: str, ref: str = "main") -> str | None:
        """A file's text as *ref* on origin holds it, or ``None`` when absent there."""
        result = git(self.origin, "show", f"{ref}:{rel}", check=False)
        return result.stdout if result.returncode == 0 else None

    def head(self, repo) -> str:
        """The commit *repo*'s HEAD names."""
        return git(repo, "rev-parse", "HEAD").stdout.strip()

    def commit_files(self, ref: str = "main") -> list[str]:
        """The paths the tip commit of *ref* on origin touched."""
        output = git(self.origin, "log", "-1", "--name-only", "--format=", ref).stdout
        return [line for line in output.splitlines() if line.strip()]

    def origin_render(self, project: str, scratch) -> tuple[str | None, str]:
        """``(README text on origin, a fresh render over a clean clone of origin)``.

        The two are equal exactly when the published block is the render of
        the published records: on a clean clone every record is tracked, so
        the kernel needs no filter, and a README absent on origin renders as
        the heading plus the block the choke point writes on first creation.
        """
        from work_repo import project_index

        scratch = Path(scratch)
        scratch.mkdir(parents=True, exist_ok=True)
        clone = scratch / f"oracle-{uuid.uuid4().hex[:8]}"
        git(scratch, "clone", "-q", str(self.origin), str(clone))
        rendered = project_index.render_project_readme(
            clone / project, heading=project.rsplit("/", 1)[-1])
        return self.origin_file(f"{project}/README.md"), rendered

    def clone_tree(self, clone) -> Schema2Tree:
        """A :class:`Schema2Tree` address book over one peer clone, so a test
        can write records there with :meth:`Schema2Tree.add`."""
        clone = Path(clone)
        return Schema2Tree(
            root=clone, manifest=clone / MANIFEST_NAME, repos=dict(self.tree.repos),
            projects={name: clone / name for name in self.tree.projects},
        )


_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def publication2(
    tmp_path,
    monkeypatch,
    *,
    projects: Mapping[str, Sequence[RecordSpec]] | None = None,
    repos: Mapping[str, str | None] | None = None,
    clones: int = 2,
) -> Publication2:
    """A bare origin, the committed schema-2 tree in ``work/`` and *clones* peers.

    *monkeypatch* is required: the git identity and config isolation of the
    push-safety fixture are applied through it for the whole test (global and
    system config off, an empty template dir, the gate in-flight guard
    disabled — a setting :func:`make_env` keeps across its own ``LMER_*``
    strip), so neither the fixture's own git calls nor the product code the
    test drives afterwards can see the developer's ``commit.gpgsign``,
    ``core.hooksPath`` or ``init.templateDir``. Each repository also gets a
    local user name and email, as ``claim_clones`` does. The primary checkout
    holds the tree :func:`build_schema2_plexus` built; every peer is a fresh
    clone of origin.
    """
    tmp_path = Path(tmp_path)
    isolate_git(monkeypatch, tmp_path)
    monkeypatch.setenv(gate_lock.GUARD_ENV, "0")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")
    root = tmp_path / "work"
    tree = build_schema2_plexus(root, projects, repos=repos)
    git(root, "init", "--initial-branch=main")
    _identify(root)
    git(root, "add", "--", MANIFEST_NAME, *tree.projects)
    git(root, "commit", "-q", "-m", "schema fixture")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "-u", "origin", "main")
    peers = []
    for index in range(1, clones + 1):
        peer = tmp_path / f"clone-{index}"
        git(tmp_path, "clone", "-q", str(origin), str(peer))
        _identify(peer)
        peers.append(peer)
    return Publication2(origin=origin, root=root, clones=tuple(peers), tree=tree)


def isolate_git(monkeypatch, tmp_path) -> None:
    """Keep every git call a test makes or drives away from the developer's config.

    The push-safety fixture's isolation: a fixed author and committer, global
    and system config off (so ``commit.gpgsign``, ``core.hooksPath`` and the
    like never reach a test repository) and an empty template directory under
    *tmp_path*. Applied through *monkeypatch* for the whole test.
    """
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    templates = Path(tmp_path) / "templates"
    templates.mkdir(exist_ok=True)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(templates))


def _identify(repo: Path) -> None:
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")


#: The environment entries that name one launch (the record a session writes in).
IDENTITY_KEYS = ("LMER_SESSION_ID", "LMER_SESSION_METADATA")


def start_session(monkeypatch, root, project: str = DEFAULT_PROJECT, *,
                  task: str = "chat") -> tuple[Path, dict]:
    """Open a fresh schema-2 launch in *project* of the tree at *root*.

    :func:`make_env` points the `work` CLI at the tree; the launch is then
    minted the way a container does it (``session_record.start_launch`` →
    ``run_state.ensure_run``), so the record is this execution's own and every
    composer the test drives afterwards writes and pushes it. The identity
    variables are registered with *monkeypatch* first — ``start_launch``
    writes them straight into ``os.environ``, and registering them is what
    makes the teardown remove them again. Returns ``(run dir, state)``.
    """
    from work_repo import run_state, session_record

    make_env(monkeypatch, root, project, task=task)
    monkeypatch.setenv("LMER_SESSION_ID", "")
    monkeypatch.setenv(session_record.METADATA_ENV, "{}")
    session_record.start_launch()
    return run_state.ensure_run()


def session_identity() -> dict[str, str]:
    """The environment entries naming the current launch, to come back to it."""
    return {key: os.environ[key] for key in IDENTITY_KEYS if key in os.environ}


def resume_session(monkeypatch, root, identity: Mapping[str, str],
                   project: str = DEFAULT_PROJECT, *, task: str = "chat") -> None:
    """Point the `work` CLI back at *root* as the launch *identity* names."""
    make_env(monkeypatch, root, project, task=task)
    for key, value in identity.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# Helpers the publication and conflict suites share
# ---------------------------------------------------------------------------


def memory_file(name: str, kind: str | None, description: str = "a fact",
                body: str = "The fact.\n") -> str:
    """A memory file's text: the frontmatter SCHEMA.md requires, plus a body.

    *kind* is the ``metadata.type`` that routes the memory to a scope
    (``project`` to the project's own scope; ``user``, ``feedback`` and
    ``reference`` to the instance scope); ``None`` writes the metadata block
    without a type, one of the unroutable shapes.
    """
    lines = ["---", f"name: {name}", f'description: "{description}"', "metadata:",
             "  node_type: memory"]
    if kind is not None:
        lines.append(f"  type: {kind}")
    lines += ["---", "", body]
    return "\n".join(lines)


def memory_env(monkeypatch, tmp_path, *, kind: str | None = "project") -> Path:
    """Enable agent-memory persistence over a tmp memory dir holding one memory.

    The memory is of *kind*; ``None`` leaves the dir empty, the shape
    ``persist_memory`` answers with ``commit_work_path([])`` (nothing to
    persist, so an empty stageable set reaches the choke point).
    """
    agent = Path(tmp_path) / "agent-memory"
    agent.mkdir(exist_ok=True)
    if kind is not None:
        (agent / "fact.md").write_text(memory_file("fact", kind), encoding="utf-8")
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(agent))
    return agent


def state_args(**overrides) -> argparse.Namespace:
    """The namespace `work state set` parses, with every flag off unless given."""
    fields = dict(action="set", phase=None, stop_reason=None, status=None,
                  critical_error=None, question=None)
    fields.update(overrides)
    return argparse.Namespace(**fields)


def row_ids(text: str | None) -> set[str]:
    """The short record ids a rendered README's rows name; empty for no README."""
    return set() if text is None else {
        line.rsplit("`", 2)[1] for line in text.splitlines() if line.startswith("| ") and " · `" in line
    }


def rebase_state(repo) -> bool:
    """Whether a rebase is paused in *repo* (either backend's state dir exists)."""
    repo = Path(repo)
    return (repo / ".git" / "rebase-merge").exists() or (repo / ".git" / "rebase-apply").exists()


def record_git_commands(monkeypatch, intercept=None) -> list[list[str]]:
    """Record every ``git_ops.run_git_command`` call the test drives.

    *intercept(cmd, cwd)* may answer a call with ``(rc, output)`` instead of
    running it, or return ``None`` to let it run. The shim has the push-safety
    suite's ``(cmd, cwd, check)`` shape and drops ``extra_env`` on purpose, so
    nothing on the publication path can rely on an overlay reaching git
    through it.
    """
    from work_repo import git_ops

    original = git_ops.run_git_command
    commands: list[list[str]] = []

    def run(cmd, cwd, check=True):
        commands.append(cmd)
        if intercept is not None:
            result = intercept(cmd, cwd)
            if result is not None:
                return result
        return original(cmd, cwd, check)

    monkeypatch.setattr(git_ops, "run_git_command", run)
    return commands


def _session_block(spec: RecordSpec) -> dict:
    session = {
        "operator": "tester",
        "hostname": "fixture.example",
        "harness": "codex",
        "model": "fixture-model",
        "effort": None,
        "config": {"preset": None, "agents": [], "taskdef": spec.taskdef or spec.slug.split("-", 1)[0]},
        "execution_hostname": "fixture.example",
        "started_at": spec.started_at,
        "pid": 2,
        "uid": spec.uid,
    }
    if spec.source_uid is not None:
        session["source_session_uid"] = spec.source_uid
    return session


def _events_for(spec: RecordSpec) -> list[dict] | None:
    if spec.signed_out is None:
        return None
    events = [
        {"ts": spec.started_at, "session": spec.uid, "type": "run_seeded"},
        {"ts": spec.started_at, "session": spec.uid, "type": "session_start",
         "data": {"session": _session_block(spec)} if spec.schema == 2 else {}},
    ]
    for index in range(spec.filler_events):
        events.append({"ts": spec.started_at, "session": spec.uid, "type": "work_log",
                       "note": f"filler event {index}"})
    if spec.signed_out:
        events.append({"ts": spec.updated, "session": spec.uid, "type": "session_end"})
    return events


def _state_document(tree: Schema2Tree, project: str, spec: RecordSpec) -> dict:
    """The state mapping ``write_state`` would dump for *spec*, in production key
    order: ``seed_state``'s fields, then ``session``, ``context`` and ``sessions``
    as the kernel adds them."""
    document: dict = {
        "schema": spec.schema,
        "slug": spec.slug,
        "name": spec.name,
        "taskdef": spec.taskdef or spec.slug.split("-", 1)[0],
        "target": "",
        "status": spec.status,
        "phase": spec.phase,
        "stop_reason": spec.stop_reason,
        "critical_error": None,
        "open_question": None,
        "goal": spec.goal,
        "estimate": None,
        "artifacts": {},
        "owner": None,
        "frozen": None,
        "created": spec.started_at,
        "updated": spec.updated,
    }
    if spec.schema == 2:
        document["session"] = _session_block(spec)
    if spec.source_uid is not None:
        document["context"] = _context_document(tree, project, spec)
    if spec.schema == 2:
        document["sessions"] = [spec.uid]
    return document


def _state_text(document: dict, spec: RecordSpec) -> str:
    """The file text: the production emitter call, then the unquoted-stamp edit."""
    text = yaml.safe_dump(document, sort_keys=False)  # the call run_state.write_state makes
    if not spec.quoted_stamps:
        text = _QUOTED_STAMP_RE.sub(r"\g<head>\g<stamp>", text)
    return text


def _context_document(tree: Schema2Tree, project: str, spec: RecordSpec) -> dict:
    """What ``_source_context`` embeds: the source's parsed state, its state file's
    digest and its last :data:`CONTEXT_EVENTS` events, in the kernel's key order."""
    source = tree.written.get((project, spec.source_uid))
    if source is None:
        stub = _stub_source_document(spec)
        source = _Written(stub, yaml.safe_dump(stub, sort_keys=False), [])
    source_path = spec.source_path
    if source_path is None:
        source_rel = f"_runs/{spec.slug}/{spec.source_uid}"
        source_dir = tree.records.get(project, {}).get(spec.source_uid)
        if source_dir is not None:
            source_rel = source_dir.relative_to(tree.projects[project]).as_posix()
        source_path = f"{project}/{source_rel}"
    return {
        "source_session_uid": spec.source_uid,
        "source_path": source_path,
        "state_sha256": hashlib.sha256(source.text.encode("utf-8")).hexdigest(),
        "state": source.document,
        "events": source.events[-CONTEXT_EVENTS:],
        "checkpoints": {},
        "unsigned_source_acknowledged": False,
    }


def _stub_source_document(spec: RecordSpec) -> dict:
    """A minimal source state for a continuation whose source was never written."""
    return {
        "schema": 2, "slug": spec.slug, "name": None, "status": "in-progress",
        "created": spec.started_at, "updated": spec.started_at,
        "session": {"uid": spec.source_uid, "started_at": spec.started_at},
    }
