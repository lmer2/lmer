"""Shared plexus fixture trees for the lmer-schema tests (issue #353).

Two trees, one API. ``build_schema_plexus`` lays out the spec's ONE form
(§2: a ``_plexus.yaml`` manifest, ``_``-owned areas, flat short-name project
dirs, ``_agents/`` at both scopes, D22 session dirs under ``_runs/<slug>/``).
``build_legacy_plexus`` lays out the same content under today's bare names
(§9: ``{host}/{project}/runs/<slug>/``, ``agent-files/``, ``mirrors/``,
``<project>/memory/``, ``specs/``, a standalone ``sources.yaml``) with NO
manifest. Both return a :class:`PlexusTree` — the consumer's address book, so
no test respells a path the builder already knows.

The validator, registry/form and work-CLI subsystems consume these trees
READ-ONLY: they import the builders (or the ``schema_plexus`` /
``legacy_plexus`` fixtures conftest re-exports) and never edit this module.
``tests/test_lmer_schema_fixtures.py`` pins the exact shape, so a consumer's
failure is never a silently drifted fixture. A consumer that needs a defect
injected (a dangling symlink, a corrupt state file) mutates ITS OWN fresh tree.

Why a tmp_path builder and not a checked-in ``tests/fixtures/plexus/`` tree
---------------------------------------------------------------------------
The other checked-in fixtures under ``tests/fixtures/`` are single files or
tiny static dirs. This tree is neither:

- it carries ``_``-prefixed names, nested ``_agents/`` dirs and a ``_runs/``
  tree of ``state.yaml``/``events.jsonl`` records — shapes the repo's own
  collection, lint and leak/security scans walk and would have to learn to
  ignore (the same reason the operational work repo is never inside the
  checkout);
- it needs symlinks (the ``_specs/`` and ``specs/`` indexes are relative
  symlinks into a run dir) and EMPTY dirs (``_taskdef/``, ``_archive/``),
  neither of which git stores faithfully across platforms and checkouts;
- every consumer mutates a copy (the validator's negative tests inject one
  defect per check family), so a fresh tree per test is required anyway — a
  builder gives isolation for free where a checked-in tree would need a copy
  step that every consumer had to remember.

The cost is that the fixture is code rather than files a reviewer can ``ls``;
:class:`PlexusTree` and the shape test are the ``ls``.

Hosts follow the repo's fixture convention (``gitlab.example.com`` and
friends); nothing here names a real forge. The builder refuses any root under
the operational work repo before it creates anything, so it cannot be the
thing that trips conftest's work-repo leak guard.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

SCHEMA_FORM = "schema"
LEGACY_FORM = "legacy"

MANIFEST_NAME = "_plexus.yaml"
INSTANCE_NAME = "example"

#: The repo-backed project (D8): a short name, its PRIMARY remote, and the
#: ADDITIONAL remotes. ``PRIMARY_REPO`` is never repeated in ``REMOTES``.
PROJECT = "lmer"
PRIMARY_REPO = "gitlab.example.com/group/project"
REMOTES = (
    "github.com/example/lmer",
    "gitlab.example.com/mirrors/lmer",
)

#: The repoless project (D19): a short name with a directory and no remote.
REPOLESS_PROJECT = "q3-capacity"
REPOLESS_DESCRIPTION = "Q3 capacity planning workstream"

#: An unnamed repo's host bucket (D9): ``{host}/{project}/`` with a dot in the
#: top-level name, project-shaped inside, absent from the registry.
BUCKET_HOST = "gitlab.example.com"
BUCKET_PROJECT = "unnamed/widget"

#: The run record: one slug, two launches. In schema form each launch is its
#: own session dir (D22); in legacy form both interleave in one shared run dir.
SLUG = "develop-issue-12"
TARGET = f"https://{PRIMARY_REPO}/-/issues/12"
TASKDEF = "develop"
SESSION_IDS = ("20260824-101500-1-11111", "20260824-113000-1-22222")
SESSION_NAME = "second-look"
SESSION_DIR_NAMES = (SESSION_IDS[0], f"{SESSION_IDS[1]}--{SESSION_NAME}")

#: One memory file per scope (D17): a ``user`` memory at the instance scope,
#: a ``project`` memory at the project scope. The legacy twin holds both in
#: ``<project>/memory/``.
USER_MEMORY_FILE = "user_operator_prefers_terse_replies.md"
PROJECT_MEMORY_FILE = "project_selfdev_pythonpath_gotcha.md"

#: §1 short-name charset and today's ``LMER_SESSION_ID`` shape (§3.5).
SHORT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
SESSION_ID_RE = re.compile(r"\d{8}-\d{6}-\d+-\d+")

_SOURCES_HOST = PRIMARY_REPO.split("/", 1)[0]

#: The §4 manifest, verbatim in shape (comments included — it is the thing a
#: reader compares against the spec).
SCHEMA_MANIFEST = f"""\
schema: 1                    # int (bool rejected); the lmer-schema version
name: {INSTANCE_NAME}
projects:                    # THE registry — identity lives here, not in paths
  {PROJECT}:
    repo: {PRIMARY_REPO}      # the primary (D8): cloned + default push target
    remotes:                              # additional remotes; repo: never repeated
      - {REMOTES[0]}
      - {REMOTES[1]}
  {REPOLESS_PROJECT}:               # a project with NO repo (D19)
    description: {REPOLESS_DESCRIPTION}
sources:                     # area mounts (sources.yaml folded in — D4)
  taskdef: {{repo: "https://{_SOURCES_HOST}/agents/taskdef.git", ref: main}}
  napkin:  {{repo: "https://{_SOURCES_HOST}/agents/napkin.git"}}
housekeeping:                # per-area TTL policy (§8); absent = defaults
  napkin: {{archive_after: 365d}}
  plans:  {{archive_after: null}}
"""

#: The legacy standalone ``sources.yaml`` (lmer_cli.container.sources schema 1).
LEGACY_SOURCES_YAML = f"""\
schema: 1
sources:
  taskdef:
    repo: https://{_SOURCES_HOST}/agents/taskdef.git
    ref: main
  napkin:
    repo: https://{_SOURCES_HOST}/agents/napkin.git
"""

_MEMORY_FILES = {
    USER_MEMORY_FILE: (
        "user",
        "Operator prefers terse replies",
        "short answers first, detail on request",
        "The operator asks for the answer first and the reasoning only when "
        "they ask for it; long preambles get skipped.\n",
    ),
    PROJECT_MEMORY_FILE: (
        "project",
        "Self-dev PYTHONPATH gotcha",
        "bare pytest imports the operational install, prefix PYTHONPATH with "
        "the checkout's src/",
        "On self-dev containers a bare `pytest` resolves the package from the "
        "operational install rather than the checkout; export PYTHONPATH to "
        "the checkout's src/ (the gate commands do this themselves).\n",
    ),
}


def _memory_text(filename):
    kind, _title, description, body = _MEMORY_FILES[filename]
    name = filename[: -len(".md")].split("_", 1)[1].replace("_", "-")
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "{description}"\n'
        "metadata:\n"
        "  node_type: memory\n"
        f"  type: {kind}\n"
        "---\n"
        "\n"
        f"{body}"
    )


def _memory_index_line(filename):
    _kind, title, description, _body = _MEMORY_FILES[filename]
    return f"- [{title}]({filename}) — {description}.\n"


def _state_yaml(*, name, status, stop_reason, frozen, created, updated, artifacts):
    """A ``docs/RUN-STATE.md`` schema-1 state file, mirroring a live one."""
    lines = [
        "schema: 1",
        f"slug: {SLUG}",
        f"name: {name if name is not None else 'null'}",
        f"taskdef: {TASKDEF}",
        f"target: {TARGET}",
        f"status: {status}",
        "phase: implement",
        f"stop_reason: {stop_reason}",
        "critical_error: null",
        "open_question: null",
        "goal: Fix issue 12 (fixture record)",
        "estimate: null",
    ]
    if artifacts:
        lines.append("artifacts:")
        lines.extend(f"  {key}: {value}" for key, value in artifacts.items())
    else:
        lines.append("artifacts: {}")
    lines.extend([
        "owner: null",
        f"frozen: {frozen}",
        f"created: '{created}'",
        f"updated: '{updated}'",
    ])
    return "\n".join(lines) + "\n"


def _events_jsonl(events):
    return "".join(json.dumps(event) + "\n" for event in events)


def _event(ts, session, kind, **extra):
    event = {"ts": ts, "session": session, "type": kind}
    event.update(extra)
    return event


_FIRST_SESSION_EVENTS = [
    _event("2026-08-24T10:15:00Z", SESSION_IDS[0], "run_seeded"),
    _event("2026-08-24T10:15:00Z", SESSION_IDS[0], "session_start"),
    _event("2026-08-24T10:16:00Z", SESSION_IDS[0], "phase", note="implement"),
    _event("2026-08-24T10:40:00Z", SESSION_IDS[0], "state_changed",
           data={"stop_reason": "complete", "status": "complete"}),
    _event("2026-08-24T10:41:00Z", SESSION_IDS[0], "session_end"),
]
_SECOND_SESSION_EVENTS = [
    _event("2026-08-24T11:30:00Z", SESSION_IDS[1], "run_seeded"),
    _event("2026-08-24T11:30:00Z", SESSION_IDS[1], "session_start"),
    _event("2026-08-24T11:31:00Z", SESSION_IDS[1], "run_named", note=SESSION_NAME),
    _event("2026-08-24T11:32:00Z", SESSION_IDS[1], "phase", note="implement"),
]

_LOG_YAML = (
    "- timestamp: '2026-08-24T10:20:00Z'\n"
    "  message: Fixture record; nothing here is real work.\n"
)

_SPEC_MD = "# Spec — issue 12\n\nFixture spec artifact; the specs index links here.\n"
_SPEC_ENTRY = f"2026-08-24-{SLUG}-spec.md"

_MIRROR_MD = (
    "# MIRROR — masterplan\n\n"
    "- **Upstream:** https://example.com/upstream/masterplan\n"
    "- **Pinned commit:** `0000000000000000000000000000000000000000`\n"
)


@dataclass(frozen=True)
class PlexusTree:
    """Where everything is in one built tree — the consumer's address book.

    Fields that a form does not have are ``None`` (legacy has no manifest, no
    instance-scope memory, no repoless project, no host bucket; schema form
    has no standalone ``sources.yaml``). ``session_dirs`` is every dir that
    holds a ``state.yaml``: the two D22 session dirs in schema form, the one
    shared run dir in legacy form.
    """

    root: Path
    form: str
    manifest: Optional[Path]
    primary_repo: str
    remotes: Tuple[str, ...]
    project_dir: Path
    repoless_dir: Optional[Path]
    bucket_dir: Optional[Path]
    runs_dir: Path
    slug: str
    session_ids: Tuple[str, ...]
    session_dirs: Tuple[Path, ...]
    specs_dir: Path
    specs_entries: Tuple[Path, ...]
    agents_claude_dir: Path
    mirrors_dir: Path
    instance_memory_dir: Optional[Path]
    project_memory_dir: Path
    memory_files: Dict[str, Path]
    taskdef_dir: Path
    napkin_dir: Path
    info_dir: Path
    knowledge_dir: Optional[Path]
    plans_dir: Optional[Path]
    archive_dir: Optional[Path]
    sources_file: Optional[Path]

    def paths(self):
        """Every path the map promises, keyed by field (tuples and the
        ``memory_files`` dict flattened) — for "does everything I promise
        exist" checks. ``None`` entries are a form's documented absences."""
        out = {}
        for field in fields(self):
            if field.name in _NON_PATH_FIELDS:
                continue
            value = getattr(self, field.name)
            if isinstance(value, tuple):
                for index, item in enumerate(value):
                    out[f"{field.name}[{index}]"] = item
            elif isinstance(value, dict):
                for key, item in value.items():
                    out[f"{field.name}[{key}]"] = item
            else:
                out[field.name] = value
        return out


#: PlexusTree fields that are identity strings, not paths.
_NON_PATH_FIELDS = frozenset({"form", "primary_repo", "remotes", "slug", "session_ids"})


def _operational_work_repo_roots():
    roots = {Path("/work")}
    configured = os.environ.get("LMER_WORK_REPO_PATH")
    if configured:
        roots.add(Path(configured))
    return roots


def _prepare_root(root):
    """Refuse the operational work repo and any non-empty dir, then mkdir."""
    root = Path(root)
    for guarded in _operational_work_repo_roots():
        if root == guarded or guarded in root.parents:
            raise ValueError(
                f"refusing to build a fixture tree under the operational work "
                f"repo ({guarded}): {root}"
            )
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"fixture root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_run_record(run_dir, *, name, status, stop_reason, frozen, events,
                      created, updated, with_spec):
    artifacts = {"spec": "spec.md"} if with_spec else {}
    _write(run_dir / "state.yaml", _state_yaml(
        name=name, status=status, stop_reason=stop_reason, frozen=frozen,
        created=created, updated=updated, artifacts=artifacts,
    ))
    _write(run_dir / "events.jsonl", _events_jsonl(events))
    _write(run_dir / "log.yaml", _LOG_YAML)
    if with_spec:
        _write(run_dir / "spec.md", _SPEC_MD)


def _write_memory_scope(scope_dir, filenames):
    scope_dir.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        _write(scope_dir / filename, _memory_text(filename))
    _write(scope_dir / "MEMORY.md", "".join(_memory_index_line(f) for f in filenames))


def _write_instance_surfaces(claude_dir, mirrors_dir, taskdef_dir, napkin_dir):
    (claude_dir / "skills").mkdir(parents=True)
    _write(claude_dir / "skills" / "hello-world" / "SKILL.md",
           "# hello-world\n\nFixture skill.\n")
    _write(mirrors_dir / "masterplan" / "MIRROR.md", _MIRROR_MD)
    taskdef_dir.mkdir(parents=True)
    _write(napkin_dir / INSTANCE_NAME / "scratch-idea.md",
           "# Scratch idea\n\nTransient note; promote or let it decay.\n")


def build_schema_plexus(root) -> PlexusTree:
    """Build the schema-form tree (§2) at *root* and return its map."""
    root = _prepare_root(root)
    _write(root / MANIFEST_NAME, SCHEMA_MANIFEST)
    _write(root / "README.md", "# example plexus\n\nContent namespace; anything human.\n")

    agents = root / "_agents"
    _write_instance_surfaces(
        agents / "claude", agents / "mirrors", root / "_taskdef", root / "_napkin",
    )
    _write(root / "_knowledge" / INSTANCE_NAME / "INDEX.md",
           "# example knowledge\n\n- nothing yet\n")
    (root / "_archive").mkdir()
    _write_memory_scope(agents / "memory", [USER_MEMORY_FILE])

    project = root / PROJECT
    runs = project / "_runs"
    group = runs / SLUG
    first = group / SESSION_DIR_NAMES[0]
    second = group / SESSION_DIR_NAMES[1]
    _write_run_record(
        first, name=None, status="complete", stop_reason="complete",
        frozen="'2026-08-24T10:16:00Z'", events=_FIRST_SESSION_EVENTS,
        created="2026-08-24T10:15:00Z", updated="2026-08-24T10:41:00Z",
        with_spec=True,
    )
    _write_run_record(
        second, name=SESSION_NAME, status="in-progress", stop_reason="null",
        frozen="'2026-08-24T11:32:00Z'", events=_SECOND_SESSION_EVENTS,
        created="2026-08-24T11:30:00Z", updated="2026-08-24T11:32:00Z",
        with_spec=False,
    )
    specs = project / "_specs"
    specs.mkdir(parents=True)
    spec_entry = specs / _SPEC_ENTRY
    spec_entry.symlink_to(f"../_runs/{SLUG}/{SESSION_DIR_NAMES[0]}/spec.md")
    _write(project / "_plans" / "PLANS.md", "# Plans — lmer\n\n(no active plans)\n")
    _write_memory_scope(project / "_agents" / "memory", [PROJECT_MEMORY_FILE])
    _write(project / "_info" / "important.md",
           "# lmer — important\n\nFixture project context.\n")
    (project / "_taskdef").mkdir()

    repoless = root / REPOLESS_PROJECT
    _write(repoless / "_plans" / "PLANS.md", "# Plans — q3-capacity\n\n(no active plans)\n")
    _write(repoless / "_info" / "important.md",
           f"# {REPOLESS_PROJECT}\n\n{REPOLESS_DESCRIPTION}.\n")

    bucket = root / BUCKET_HOST / BUCKET_PROJECT
    (bucket / "_runs").mkdir(parents=True)
    _write(bucket / "_info" / "important.md",
           "# unnamed\n\nWork parked here until a human names this repo (D9).\n")

    return PlexusTree(
        root=root,
        form=SCHEMA_FORM,
        manifest=root / MANIFEST_NAME,
        primary_repo=PRIMARY_REPO,
        remotes=REMOTES,
        project_dir=project,
        repoless_dir=repoless,
        bucket_dir=bucket,
        runs_dir=runs,
        slug=SLUG,
        session_ids=SESSION_IDS,
        session_dirs=(first, second),
        specs_dir=specs,
        specs_entries=(spec_entry,),
        agents_claude_dir=agents / "claude",
        mirrors_dir=agents / "mirrors",
        instance_memory_dir=agents / "memory",
        project_memory_dir=project / "_agents" / "memory",
        memory_files={
            "user": agents / "memory" / USER_MEMORY_FILE,
            "project": project / "_agents" / "memory" / PROJECT_MEMORY_FILE,
        },
        taskdef_dir=root / "_taskdef",
        napkin_dir=root / "_napkin",
        info_dir=project / "_info",
        knowledge_dir=root / "_knowledge",
        plans_dir=project / "_plans",
        archive_dir=root / "_archive",
        sources_file=None,
    )


def build_legacy_plexus(root) -> PlexusTree:
    """Build the legacy-form twin (§9 bare names, no manifest) at *root*."""
    root = _prepare_root(root)
    _write(root / "sources.yaml", LEGACY_SOURCES_YAML)
    _write(root / "README.md", "# work repo\n\nLegacy layout; see docs/SCHEMA.md.\n")
    _write_instance_surfaces(
        root / "agent-files" / "claude", root / "mirrors", root / "taskdef", root / "napkin",
    )

    host, path = PRIMARY_REPO.split("/", 1)
    project = root / host / path
    runs = project / "runs"
    run_dir = runs / SLUG
    # One shared dir, both sessions' history interleaved (§3.5 legacy).
    _write_run_record(
        run_dir, name=None, status="in-progress", stop_reason="null",
        frozen="'2026-08-24T10:16:00Z'",
        events=_FIRST_SESSION_EVENTS + _SECOND_SESSION_EVENTS,
        created="2026-08-24T10:15:00Z", updated="2026-08-24T11:32:00Z",
        with_spec=True,
    )
    specs = project / "specs"
    specs.mkdir(parents=True)
    spec_entry = specs / _SPEC_ENTRY
    spec_entry.symlink_to(f"../runs/{SLUG}/spec.md")
    # Pre-D17: every memory type lands in the project dir.
    _write_memory_scope(project / "memory", [PROJECT_MEMORY_FILE, USER_MEMORY_FILE])
    _write(project / "info" / "important.md",
           "# lmer — important\n\nFixture project context.\n")
    (project / "taskdef").mkdir()

    return PlexusTree(
        root=root,
        form=LEGACY_FORM,
        manifest=None,
        primary_repo=PRIMARY_REPO,
        remotes=REMOTES,
        project_dir=project,
        repoless_dir=None,
        bucket_dir=None,
        runs_dir=runs,
        slug=SLUG,
        session_ids=SESSION_IDS,
        session_dirs=(run_dir,),
        specs_dir=specs,
        specs_entries=(spec_entry,),
        agents_claude_dir=root / "agent-files" / "claude",
        mirrors_dir=root / "mirrors",
        instance_memory_dir=None,
        project_memory_dir=project / "memory",
        memory_files={
            "user": project / "memory" / USER_MEMORY_FILE,
            "project": project / "memory" / PROJECT_MEMORY_FILE,
        },
        taskdef_dir=root / "taskdef",
        napkin_dir=root / "napkin",
        info_dir=project / "info",
        knowledge_dir=None,
        plans_dir=None,
        archive_dir=None,
        sources_file=root / "sources.yaml",
    )


#: §7's obsolete derived spellings: ``(taskdef, target, slug)``. Each slug is
#: the pre-A1 derivation of its own target, so each must be folded into the
#: bare taskdef group by ``work migrate``.
PLAIN_SESSION_GROUPS = (
    ("chat", ".", "chat-."),
    ("chat", "https://gitlab.example.com/acme/widgets", "chat-widgets"),
    ("chat", "https://gitlab.example.com/acme/widgets.git", "chat-widgets.git"),
)

#: Astra R1's release record in both states: an empty target that derives the
#: bare taskdef before version recording, and its re-slugged successor after.
#: Neither is a plain-session leftover; neither may ever be flagged or moved.
RELEASE_RECORDS = (
    {"taskdef": "release", "target": "", "slug": "release"},
    {"taskdef": "release", "target": "", "slug": "release-v0.10.2",
     "reslugged_from": ["release"]},
)


def _plain_session_state_yaml(record) -> str:
    """A schema-1 state file carrying exactly the predicate's inputs."""
    lines = [
        "schema: 1",
        f"slug: {record['slug']}",
        "name: null",
        f"taskdef: {record['taskdef']}",
        f"target: {record.get('target', '')!r}",
        "status: complete",
        "phase: implement",
        "stop_reason: complete",
        "critical_error: null",
        "open_question: null",
        "goal: fixture",
        "estimate: null",
        "artifacts: {}",
        "owner: null",
        "frozen: '2026-09-01T10:00:00Z'",
        "created: '2026-09-01T10:00:00Z'",
        "updated: '2026-09-01T10:00:00Z'",
    ]
    reslugged = record.get("reslugged_from")
    if reslugged:
        lines.append("reslugged_from:")
        lines.extend(f"  - {slug}" for slug in reslugged)
    return "\n".join(lines) + "\n"


def plant_plain_session_records(project_dir, *, area="_runs", nested=True,
                                session_id=None):
    """Plant §7's plain-session groups and R1's release record under
    *project_dir*, returning ``{slug: [state.yaml, ...]}``.

    *area* is the run area's name (``_runs`` in schema form, ``runs`` in a
    legacy one) and *nested* whether each group holds a session directory
    (schema form) or the state file sits directly in the group (legacy form).
    The three obsolete spellings must be folded; R1's release record must
    never be touched in either state. The existing builders and
    ``tests/test_lmer_schema_fixtures.py`` are left untouched.
    """
    planted = {}
    records = [
        {"taskdef": taskdef, "target": target, "slug": slug}
        for taskdef, target, slug in PLAIN_SESSION_GROUPS
    ]
    records.extend(dict(record) for record in RELEASE_RECORDS)
    for index, record in enumerate(records):
        # A distinct, valid session id per group: one shared id would add a
        # `duplicate historical session ID` blocker on top of the fold ones.
        sid = session_id or f"20260901-1000{index:02d}-1-{index}"
        group = Path(project_dir) / area / record["slug"]
        rdir = group / sid if nested else group
        rdir.mkdir(parents=True, exist_ok=True)
        _write(rdir / "state.yaml", _plain_session_state_yaml(record))
        _write(rdir / "events.jsonl", "")
        planted.setdefault(record["slug"], []).append(rdir / "state.yaml")
    return planted


@pytest.fixture
def schema_plexus(tmp_path) -> PlexusTree:
    """A fresh schema-form plexus (§2) under this test's tmp_path."""
    return build_schema_plexus(tmp_path / "plexus-schema")


@pytest.fixture
def legacy_plexus(tmp_path) -> PlexusTree:
    """A fresh legacy-form plexus (§9 bare names, no manifest) under tmp_path."""
    return build_legacy_plexus(tmp_path / "plexus-legacy")
