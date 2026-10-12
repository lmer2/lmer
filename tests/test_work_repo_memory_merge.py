"""Real-git merge behaviour of ``work memory persist`` (#369 with #162, §13.D).

The memory-merge cases the other two memory suites cannot run: they stub
``commit_work_path`` and ``check_commit_ready`` on plain directories, so a
persist there never meets another clone's push. Here a bare origin and one
clone per session stand in for the shared work repo, and ``persist_memory``
runs end to end — copy, commit, ``pull --rebase``, push, publication check —
with nothing stubbed. ``tests/plexus_fixtures.py`` is used read-only: the
seed tree is built by its builders and committed as it comes.

Three kinds of test live here (task D.gap-analysis, wave 1):

1. the harness itself, proven by a round trip through origin;
2. PASSING pins of present behaviour that astra R2 relies on;
3. ``xfail(strict=True)`` tests, one tag per MISSING row of the gap table
   below. Each later D task removes the markers for its own tag; a marker
   that starts passing early fails the suite, which is what ``strict`` is
   for. ``raises=AssertionError`` keeps a harness error from hiding behind
   an expected failure.

Confirmed gap table — ``src/work_repo/memory.py`` @ 4463552a
=============================================================
Re-checked at the integration-branch head for this task: HEAD is 4463552a
itself and ``git diff HEAD -- src/work_repo/memory.py`` is empty, so every
line reference below still holds. **No row is struck** — all five MISSING
halves are open.

KEEP, untouched and pinned (R2): the restore baseline
(``read_restore_manifest`` :722 / ``write_restore_manifest`` :759); the
hash-guarded copy (``_copy_files`` :1171); digest-checked deletion
(``_restored_then_removed`` :1234); the baseline recording what the session
holds across persists (``_write_scope`` :1753-1795); scope shadowing
(``_shadowed`` :1847); scoped retirement with archive tombstones
(``forget_memory`` :489 / ``revive_memory`` :585); the checkout -> memory
lock order (``_memory_retirement_lock`` :415); pending-retirement
publication. Call shapes the pins depend on: ``_persist_scopes(source,
scopes, retired)`` positionally (test_work_repo_memory_scopes.py:1708);
``commit_work_path(target, message)`` positionally (:1714 patches it with
``lambda *_args``); ``check_commit_ready`` stubbed on plain non-git trees;
the names test_schema_v2_final_boundaries.py:7-9 imports (``schema_tree``,
``schema2_tree``, ``_memory_file``, ``_index_line``, ``_persist``). KEEP
also, because other areas call them unchanged: ``_entry_targets``,
``_is_tombstone``, ``_rewrite_index_target``, ``_memory_retirement_lock``
(C.housekeep-index), ``scope_for_memory`` and the ``MEMORY.md`` index-line
splitter (B.migrate-links); their signatures and semantics do not change
in D.

(a) -> D.baseline. PRESENT: the manifest beside the memory dir, validated
    for ``version`` (:744) and ``memory_dir`` (:746) only. MISSING: no
    session id is recorded (:766-770 writes version, memory_dir, scopes) or
    checked — ``LMER_SESSION_ID`` does not occur in memory.py — so a memory
    dir that outlives its session hands the next session a foreign baseline.
(b) -> D.merge-delta. PRESENT: ``check_commit_ready`` refuses an in-progress
    rebase or merge before copying (:1527); ``commit_work_path``'s
    post-commit ``pull --rebase --autostash`` aborts a conflicted rebase and
    keeps the local commit (git_ops.py:1027-1044). MISSING: no pull BEFORE
    the merge — ``_persist_scopes`` (:1560) merges against a possibly stale
    checkout, so another clone's add, edit or delete is invisible until it
    collides at the post-commit rebase. There is no ``.conflict-<sid>`` copy
    (no ``conflict-``, ``pull`` or ``fetch`` in memory.py), and the
    conflicting memory commit stays local and makes every later publication
    from the shared checkout fail again.
(c) -> D.baseline. PRESENT: a missing, unreadable or wrong-version manifest
    deletes nothing (:733-747); a failed restore leaves ``{}`` (:1312; the
    snapshot is written only after success, :1338/:1362); deletions are a
    subset of the manifest's files; keys that escape the scope are dropped
    (:782); restore holds the checkout lock (``@work_transaction`` ->
    transactions.py:160), so no lmer writer can land between listing and
    snapshot; retirement enforcement (:1744) deletes without a baseline BY
    DESIGN, because it carries out an explicit forget (pin
    ``test_persist_without_a_restore_baseline_cannot_revive_retired_content``).
    MISSING: the explicit flag (``work memory persist`` takes only
    ``--message``, cli.py:242-250) and any report of what a persist without
    a baseline declined to delete.
(d) -> D.baseline. PRESENT: only ``Merged N agent memory file(s)`` (:1550)
    and the untouched-scope warning (:1679). MISSING: every unlink in
    ``_write_scope`` (:1747 retirement, :1749 restored-then-removed) is
    silent.
(e) -> D.index. PRESENT: the three-way line merge (``_merged_index_text``
    :1053) keeps headings, commentary and tombstones verbatim; live entries
    for retired keys are filtered (:693). MISSING: entry lines are never
    reconciled with live files. A dangling entry survives
    (``agents.memory-entry-without-file``, an ERROR in schema form,
    SCHEMA.md:834), a live file with no line stays unindexed, and a session
    with no index (:1705 passes ``None``; :1770 then leaves the scope's
    index untouched).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Optional

import pytest

from lmer_schema.checks.runner import validate
from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    PRIMARY_REPO,
    PROJECT_MEMORY_FILE,
    USER_MEMORY_FILE,
    build_legacy_plexus,
    build_schema_plexus,
)
from work_repo import cli, memory

SCHEMA2 = "schema2"
LEGACY = "legacy"

#: Session ids of either form (§13.D (a)): a 32-hex uid on schema 2
#: (``work_repo.session_record.UID_RE``), ``YYYYMMDD-HHMMSS-pid-rand`` on
#: schema 1 (``tests.plexus_fixtures.SESSION_ID_RE``). Three per form: two
#: sessions plus a successor that inherits a session's memory dir.
SESSION_IDS = {
    SCHEMA2: (
        "0123456789abcdef0123456789abcdef",
        "fedcba9876543210fedcba9876543210",
        "00112233445566778899aabbccddeeff",
    ),
    LEGACY: (
        "20261007-101500-4242-11111",
        "20261007-113000-4243-22222",
        "20261007-120000-4244-33333",
    ),
}

#: The frontmatter ``name`` the fixture's project memory declares.
PROJECT_MEMORY_NAME = "selfdev-pythonpath-gotcha"

ENTRY_WITHOUT_FILE = "agents.memory-entry-without-file"
FILE_WITHOUT_ENTRY = "agents.memory-file-without-entry"


def git(cwd, *args, check=True):
    """Run git in *cwd*; assert success unless *check* is off."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30
    )
    if check:
        assert result.returncode == 0, " ".join(args) + "\n" + result.stdout + result.stderr
    return result


def _memory_file(name, kind, body="The fact.\n"):
    """A memory file's text: the frontmatter SCHEMA.md requires, plus a body."""
    return (
        "---\n"
        f"name: {name}\n"
        'description: "a fact"\n'
        "metadata:\n"
        "  node_type: memory\n"
        f"  type: {kind}\n"
        "---\n"
        "\n"
        f"{body}"
    )


def _index_line(title, filename):
    return f"- [{title}]({filename}) — a fact.\n"


# ---------------------------------------------------------------------------
# The harness: a bare origin, one clone per session
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    """One container's view: its own clone, memory dir and session id.

    The memory dir has its own parent (``<name>/memory``) because the restore
    manifest is written BESIDE it (:func:`memory.restore_manifest_path`): two
    sessions sharing a parent would share a baseline and prove nothing.
    """

    name: str
    sid: str
    clone: Path
    memory_dir: Path

    @property
    def manifest(self) -> Path:
        return self.memory_dir.parent / memory.RESTORE_MANIFEST_NAME

    @property
    def git_dir(self) -> Path:
        return self.clone / ".git"


class Plexus:
    """A seed tree from the shared builders, pushed to a bare origin.

    ``start`` clones it the way a container start does; ``enter`` points the
    work CLI at one session; ``publish`` plays another writer landing a hand
    edit on origin; ``published`` reads origin's ``main`` without any clone.
    """

    def __init__(self, form: str, root: Path, monkeypatch) -> None:
        self.form = form
        self.root = root
        self.monkeypatch = monkeypatch
        self.origin = root / "origin.git"
        root.mkdir(parents=True)
        seed = root / "seed"
        self.tree = (
            build_legacy_plexus(seed) if form == LEGACY else build_schema_plexus(seed)
        )
        if form == SCHEMA2:
            manifest = self.tree.manifest
            manifest.write_text(manifest.read_text().replace("schema: 1", "schema: 2", 1))
        self.project_rel = self.tree.project_memory_dir.relative_to(seed).as_posix()
        self.instance_rel: Optional[str] = (
            self.tree.instance_memory_dir.relative_to(seed).as_posix()
            if self.tree.instance_memory_dir is not None
            else None
        )
        git(root, "init", "--bare", "--initial-branch=main", str(self.origin))
        git(seed, "init", "--initial-branch=main")
        self._identify(seed)
        git(seed, "add", "--all")
        git(seed, "commit", "-q", "-m", "seed plexus")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "-u", "origin", "main")
        self._sids: Iterator[str] = iter(SESSION_IDS[form])
        self._writers = 0

    @staticmethod
    def _identify(clone: Path) -> None:
        """Local git user config, per clone — never the developer's own."""
        git(clone, "config", "user.name", "Memory Merge Test")
        git(clone, "config", "user.email", "memory-merge@example.com")

    def fresh_sid(self) -> str:
        return next(self._sids)

    def start(self, name: str) -> Session:
        """Start a session: a fresh clone, an empty memory dir in its own
        parent, and the next session id of this form."""
        home = self.root / name
        clone = home / "work"
        git(self.root, "clone", "-q", str(self.origin), str(clone))
        self._identify(clone)
        memory_dir = home / "memory"
        memory_dir.mkdir(parents=True)
        return Session(name, self.fresh_sid(), clone, memory_dir)

    def enter(self, session: Session) -> Session:
        """Point the work CLI at *session*: its clone, memory dir and id."""
        host, project = PRIMARY_REPO.split("/", 1)
        for key, value in {
            "LMER_WORK_REPO_PATH": session.clone,
            "LMER_REPO_HOST": host,
            "LMER_REPO_PROJECT": project,
            "LMER_REPO_URL": f"https://{PRIMARY_REPO}",
            "LMER_AGENT_MEMORY_DIR": session.memory_dir,
            "LMER_SESSION_ID": session.sid,
            "LMER_PERSIST_AGENT_MEMORY": "1",
        }.items():
            self.monkeypatch.setenv(key, str(value))
        return session

    def scope_rel(self, scope: str = memory.PROJECT_SCOPE) -> str:
        rel = self.project_rel if scope == memory.PROJECT_SCOPE else self.instance_rel
        assert rel is not None, f"the {self.form} form has no {scope} scope"
        return rel

    def scope(self, session: Session, scope: str = memory.PROJECT_SCOPE) -> Path:
        return session.clone / self.scope_rel(scope)

    def fixture_rels(self):
        """Root-relative paths of the two memory files the seed tree holds."""
        return [
            path.relative_to(self.tree.root).as_posix()
            for path in self.tree.memory_files.values()
        ]

    def published(self, rel: str) -> Optional[str]:
        """The text of *rel* on origin's ``main``, or None when it is not there."""
        result = git(self.origin, "show", f"main:{rel}", check=False)
        return result.stdout if result.returncode == 0 else None

    def publish(self, edit: Callable[[Path], None], message: str = "another writer") -> None:
        """Land a hand edit on origin as another writer would: a scratch
        clone, *edit(clone)* applied to it, one commit, one push."""
        self._writers += 1
        clone = self.root / f"writer-{self._writers}"
        git(self.root, "clone", "-q", str(self.origin), str(clone))
        self._identify(clone)
        edit(clone)
        git(clone, "add", "--all")
        git(clone, "commit", "-q", "-m", message)
        git(clone, "push", "-q", "origin", "main")


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path):
    """No ambient run context; git reads no user or system config."""
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("LMER_GATE_INFLIGHT_GUARD", "0")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    templates = tmp_path / "git-templates"
    templates.mkdir()
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(templates))


@pytest.fixture(params=[SCHEMA2, LEGACY])
def plexus(request, tmp_path, monkeypatch, _isolated_git):
    """The same guarantees on BOTH forms; a test's id names the form."""
    return Plexus(request.param, tmp_path / "plexus", monkeypatch)


@pytest.fixture
def schema2_plexus(tmp_path, monkeypatch, _isolated_git):
    """Schema 2 only: retirement and the validator's ERROR tier live here."""
    return Plexus(SCHEMA2, tmp_path / "plexus", monkeypatch)


def _add(session: Session, stem: str, kind: str) -> None:
    """A new memory in the agent's dir plus its index line."""
    (session.memory_dir / f"{stem}.md").write_text(_memory_file(stem, kind))
    index = session.memory_dir / memory.MEMORY_INDEX
    text = index.read_text() if index.is_file() else ""
    index.write_text(text + _index_line(stem, f"{stem}.md"))


def _remove(session: Session, filename: str) -> None:
    """Delete a memory from the agent's dir and drop its index line."""
    (session.memory_dir / filename).unlink()
    index = session.memory_dir / memory.MEMORY_INDEX
    if index.is_file():
        index.write_text(
            "".join(
                line
                for line in index.read_text().splitlines(keepends=True)
                if f"({filename})" not in line
            )
        )


def _rebase_in_progress(session: Session) -> bool:
    return any(
        (session.git_dir / state).exists() for state in ("rebase-merge", "rebase-apply")
    )


def _entries(directory: Path) -> set:
    """The entry targets of one scope's index (a KEEP helper reads them)."""
    index = directory / memory.MEMORY_INDEX
    targets: set = set()
    for line in index.read_text().splitlines() if index.is_file() else []:
        targets.update(memory._entry_targets(line))
    return targets


def _live(directory: Path) -> set:
    return {
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".md" and path.name != memory.MEMORY_INDEX
    }


# ---------------------------------------------------------------------------
# 1. The harness
# ---------------------------------------------------------------------------


class TestHarness:
    def test_a_persist_lands_on_origin_and_the_next_session_restores_it(self, plexus):
        alpha = plexus.enter(plexus.start("alpha"))
        assert memory.restore_memory() == 0
        assert (alpha.memory_dir / PROJECT_MEMORY_FILE).is_file()
        assert (alpha.memory_dir / USER_MEMORY_FILE).is_file()
        assert alpha.manifest.is_file()
        assert alpha.manifest.parent == alpha.memory_dir.parent

        _add(alpha, "alpha_note", "project")
        assert memory.persist_memory("alpha") == 0

        rel = f"{plexus.project_rel}/alpha_note.md"
        assert plexus.published(rel) == _memory_file("alpha_note", "project")
        assert "(alpha_note.md)" in plexus.published(f"{plexus.project_rel}/MEMORY.md")
        assert not _rebase_in_progress(alpha)

        beta = plexus.enter(plexus.start("beta"))
        assert beta.sid != alpha.sid and beta.clone != alpha.clone
        assert beta.manifest != alpha.manifest
        assert memory.restore_memory() == 0
        assert (beta.memory_dir / "alpha_note.md").read_text() == _memory_file(
            "alpha_note", "project"
        )
        assert "(alpha_note.md)" in (beta.memory_dir / memory.MEMORY_INDEX).read_text()


# ---------------------------------------------------------------------------
# 2. Present behaviour R2 relies on — PASSING pins
# ---------------------------------------------------------------------------


class TestPresentBehaviourPinnedForR2:
    def test_a_restore_that_fails_mid_copy_leaves_an_empty_baseline_and_the_next_persist_deletes_nothing(
        self, plexus, monkeypatch
    ):
        """:1312 writes ``{}`` before copying; the snapshot comes only after
        success (:1338/:1362). A session handed a partial dir has no record,
        and no record deletes nothing — the files it was never given stay."""
        alpha = plexus.enter(plexus.start("alpha"))
        real_copy = shutil.copy2
        failing = {"on": True}

        def copy2(src, dst, *args, **kwargs):
            if failing["on"] and Path(dst).name == PROJECT_MEMORY_FILE:
                raise OSError("simulated copy failure")
            return real_copy(src, dst, *args, **kwargs)

        monkeypatch.setattr(memory.shutil, "copy2", copy2)
        assert memory.restore_memory() == 1
        failing["on"] = False

        assert memory.read_restore_manifest() == {}
        assert json.loads(alpha.manifest.read_text())["scopes"] == {}
        assert not (alpha.memory_dir / PROJECT_MEMORY_FILE).exists()

        _add(alpha, "alpha_note", "project")
        assert memory.persist_memory("alpha") == 0

        for rel in plexus.fixture_rels():
            assert (alpha.clone / rel).is_file(), rel
            assert plexus.published(rel) == (plexus.tree.root / rel).read_text(), rel
        assert plexus.published(f"{plexus.project_rel}/alpha_note.md") is not None
        assert not _rebase_in_progress(alpha)

    def test_a_manifest_naming_a_subset_can_delete_only_that_subset(self, plexus):
        """A short baseline: the record names one of the two files it
        restored. Removing both locally deletes only the named one — the
        deletion set is a subset of the manifest's files (:1234, :1748)."""
        beta = plexus.enter(plexus.start("beta"))
        assert memory.restore_memory() == 0
        _add(beta, "second", "project")
        assert memory.persist_memory("beta") == 0

        alpha = plexus.enter(plexus.start("alpha"))
        assert memory.restore_memory() == 0
        data = json.loads(alpha.manifest.read_text())
        entry = data["scopes"][memory.PROJECT_SCOPE]
        assert {PROJECT_MEMORY_FILE, "second.md"} <= set(entry["files"])
        entry["files"] = {"second.md": entry["files"]["second.md"]}
        entry["index"] = [line for line in entry["index"] if "(second.md)" in line]
        alpha.manifest.write_text(json.dumps(data))

        _remove(alpha, PROJECT_MEMORY_FILE)
        _remove(alpha, "second.md")
        _add(alpha, "alpha_note", "project")
        assert memory.persist_memory("alpha") == 0

        project = plexus.scope(alpha)
        assert not (project / "second.md").exists()
        assert plexus.published(f"{plexus.project_rel}/second.md") is None
        assert (project / PROJECT_MEMORY_FILE).is_file()
        assert plexus.published(f"{plexus.project_rel}/{PROJECT_MEMORY_FILE}") is not None
        assert plexus.published(f"{plexus.project_rel}/alpha_note.md") is not None
        assert not _rebase_in_progress(alpha)


# ---------------------------------------------------------------------------
# 3. The MISSING rows — strict xfails, one tag each
# ---------------------------------------------------------------------------


class TestGapABaselineKeyedBySession:
    def test_a_manifest_recorded_under_another_session_id_deletes_nothing(self, plexus):
        alpha = plexus.enter(plexus.start("alpha"))
        assert memory.restore_memory() == 0
        data = json.loads(alpha.manifest.read_text())
        data["session"] = alpha.sid  # explicit, for the day the writer records one
        alpha.manifest.write_text(json.dumps(data))

        # The memory dir outlives its session: a new launch, same dir, same
        # manifest, another id.
        successor = plexus.enter(replace(alpha, sid=plexus.fresh_sid()))
        assert successor.sid != alpha.sid
        _remove(successor, PROJECT_MEMORY_FILE)
        _add(successor, "successor_note", "project")
        assert memory.persist_memory("successor") == 0

        rel = f"{plexus.project_rel}/{PROJECT_MEMORY_FILE}"
        assert (successor.clone / rel).is_file()
        assert plexus.published(rel) is not None

    def test_a_manifest_with_no_session_id_against_a_set_id_deletes_nothing(self, plexus):
        alpha = plexus.enter(plexus.start("alpha"))
        assert memory.restore_memory() == 0
        data = json.loads(alpha.manifest.read_text())
        data.pop("session", None)  # an older container's record
        alpha.manifest.write_text(json.dumps(data))

        _remove(alpha, PROJECT_MEMORY_FILE)
        _add(alpha, "alpha_note", "project")
        assert memory.persist_memory("alpha") == 0

        rel = f"{plexus.project_rel}/{PROJECT_MEMORY_FILE}"
        assert (alpha.clone / rel).is_file()
        assert plexus.published(rel) is not None


class TestGapBMergeDelta:
    def test_a_concurrent_add_across_clones_keeps_both_memories_and_both_index_lines(
        self, plexus
    ):
        alpha, beta = plexus.start("alpha"), plexus.start("beta")
        for session in (alpha, beta):
            plexus.enter(session)
            assert memory.restore_memory() == 0

        plexus.enter(beta)
        _add(beta, "beta_note", "project")
        assert memory.persist_memory("beta") == 0

        plexus.enter(alpha)
        _add(alpha, "alpha_note", "project")
        assert memory.persist_memory("alpha") == 0

        assert not _rebase_in_progress(alpha)
        for stem in ("alpha_note", "beta_note"):
            rel = f"{plexus.project_rel}/{stem}.md"
            assert plexus.published(rel) == _memory_file(stem, "project"), rel
            assert (alpha.clone / rel).is_file(), rel
        index = plexus.published(f"{plexus.project_rel}/MEMORY.md")
        assert "(alpha_note.md)" in index and "(beta_note.md)" in index, index
        assert index == (plexus.scope(alpha) / memory.MEMORY_INDEX).read_text()

    def test_an_upstream_change_followed_by_a_local_delete_keeps_the_upstream_bytes(
        self, plexus
    ):
        alpha, beta = plexus.start("alpha"), plexus.start("beta")
        for session in (alpha, beta):
            plexus.enter(session)
            assert memory.restore_memory() == 0

        upstream = _memory_file(PROJECT_MEMORY_NAME, "project", body="rewritten upstream\n")
        plexus.enter(beta)
        (beta.memory_dir / PROJECT_MEMORY_FILE).write_text(upstream)
        assert memory.persist_memory("beta") == 0
        rel = f"{plexus.project_rel}/{PROJECT_MEMORY_FILE}"
        assert plexus.published(rel) == upstream

        plexus.enter(alpha)
        _remove(alpha, PROJECT_MEMORY_FILE)
        _add(alpha, "alpha_note", "project")
        assert memory.persist_memory("alpha") == 0

        assert not _rebase_in_progress(alpha)
        assert (alpha.clone / rel).read_text() == upstream
        assert plexus.published(rel) == upstream

    def test_a_forced_rebase_conflict_leaves_a_conflict_copy_and_no_rebase_state(
        self, plexus, monkeypatch
    ):
        """Both sessions edit one memory; the sibling publishes in the window
        between this session's scope merge and its commit, so the conflict is
        forced at the post-commit rebase whether or not a pull precedes the
        merge. The wrapper only sequences the sibling's REAL persist before
        the REAL ``commit_work_path``; nothing is stubbed."""
        alpha, beta = plexus.start("alpha"), plexus.start("beta")
        for session in (alpha, beta):
            plexus.enter(session)
            assert memory.restore_memory() == 0

        theirs = _memory_file(PROJECT_MEMORY_NAME, "project", body="beta's version\n")
        mine = _memory_file(PROJECT_MEMORY_NAME, "project", body="alpha's version\n")
        (beta.memory_dir / PROJECT_MEMORY_FILE).write_text(theirs)
        (alpha.memory_dir / PROJECT_MEMORY_FILE).write_text(mine)

        real_commit = memory.commit_work_path
        raced = []

        def commit_after_the_sibling_publishes(*args, **kwargs):
            if not raced:
                raced.append(True)
                plexus.enter(beta)
                assert memory.persist_memory("beta") == 0
                plexus.enter(alpha)
            return real_commit(*args, **kwargs)

        monkeypatch.setattr(memory, "commit_work_path", commit_after_the_sibling_publishes)
        plexus.enter(alpha)
        rc = memory.persist_memory("alpha")

        assert raced
        assert rc != 0
        assert not _rebase_in_progress(alpha)
        copies = [
            path for path in alpha.memory_dir.iterdir()
            if f".conflict-{alpha.sid}" in path.name
        ]
        assert copies, sorted(path.name for path in alpha.memory_dir.iterdir())
        # The copy is what the session has NOT seen — the sibling's published
        # version; its own version is still its own file.
        assert copies[0].read_text() == theirs
        assert (alpha.memory_dir / PROJECT_MEMORY_FILE).read_text() == mine
        # The local memory commit is reverted and dropped, so nothing of it
        # is left to replay and the next publication from the shared checkout
        # goes through, landing on upstream's bytes.
        rel = f"{plexus.project_rel}/{PROJECT_MEMORY_FILE}"
        unpublished = git(alpha.clone, "rev-list", "origin/main..HEAD").stdout
        assert unpublished == "", unpublished
        (alpha.clone / "after-conflict.txt").write_text("next writer\n")
        assert real_commit("after-conflict.txt", "next writer") == 0
        assert plexus.published("after-conflict.txt") == "next writer\n"
        assert plexus.published(rel) == theirs
        assert (alpha.clone / rel).read_text() == theirs


class TestDroppedCommitRecovery:
    """A dropped memory commit loses nothing: everything it carried that did
    not conflict is published by the next persist, and the restore manifest
    is rolled back so those adds are not mistaken for published ones."""

    @staticmethod
    def _race(plexus, monkeypatch, alpha, beta):
        real_commit = memory.commit_work_path
        raced = []

        def commit_after_the_sibling_publishes(*args, **kwargs):
            if not raced:
                raced.append(True)
                plexus.enter(beta)
                assert memory.persist_memory("beta") == 0
                plexus.enter(alpha)
            return real_commit(*args, **kwargs)

        monkeypatch.setattr(memory, "commit_work_path", commit_after_the_sibling_publishes)
        plexus.enter(alpha)
        rc = memory.persist_memory("alpha")
        monkeypatch.setattr(memory, "commit_work_path", real_commit)
        assert raced
        return rc

    def _restored_pair(self, plexus):
        alpha, beta = plexus.start("alpha"), plexus.start("beta")
        for session in (alpha, beta):
            plexus.enter(session)
            assert memory.restore_memory() == 0
        return alpha, beta

    def test_an_index_only_conflict_is_reported_and_the_retry_publishes_both(
        self, plexus, monkeypatch, capsys
    ):
        alpha, beta = self._restored_pair(plexus)
        _add(alpha, "alpha_new", "project")
        _add(beta, "beta_new", "project")

        assert self._race(plexus, monkeypatch, alpha, beta) != 0
        assert "commit was dropped" in capsys.readouterr().err
        assert memory.persist_memory("alpha retry") == 0

        index = plexus.published(f"{plexus.project_rel}/{memory.MEMORY_INDEX}") or ""
        for stem in ("alpha_new", "beta_new"):
            assert plexus.published(f"{plexus.project_rel}/{stem}.md") is not None, stem
            assert f"({stem}.md)" in index, index

    def test_a_non_conflicting_memory_in_the_dropped_commit_is_published_on_retry(
        self, plexus, monkeypatch
    ):
        alpha, beta = self._restored_pair(plexus)
        theirs = _memory_file(PROJECT_MEMORY_NAME, "project", body="beta's version\n")
        (beta.memory_dir / PROJECT_MEMORY_FILE).write_text(theirs)
        (alpha.memory_dir / PROJECT_MEMORY_FILE).write_text(
            _memory_file(PROJECT_MEMORY_NAME, "project", body="alpha's version\n")
        )
        _add(alpha, "alpha_new", "project")

        assert self._race(plexus, monkeypatch, alpha, beta) != 0
        # The agent reconciles from what persist left it — the copy of
        # upstream's version, its only source for the sibling's edit.
        (copy,) = [p for p in alpha.memory_dir.iterdir() if ".conflict-" in p.name]
        assert copy.read_text() == theirs
        (alpha.memory_dir / PROJECT_MEMORY_FILE).write_text(copy.read_text())
        copy.unlink()
        assert memory.persist_memory("alpha retry") == 0

        assert plexus.published(f"{plexus.project_rel}/alpha_new.md") is not None
        assert plexus.published(f"{plexus.project_rel}/{PROJECT_MEMORY_FILE}") == theirs

    def test_a_fresh_start_baseline_is_a_baseline_and_the_flag_deletes_nothing_unseen(
        self, plexus
    ):
        def wipe(clone: Path) -> None:
            for rel in plexus.fixture_rels():
                (clone / rel).unlink()
            for rel in (plexus.project_rel, plexus.instance_rel):
                if rel and (clone / rel / memory.MEMORY_INDEX).exists():
                    (clone / rel / memory.MEMORY_INDEX).unlink()

        plexus.publish(wipe, "wipe memory")
        alpha, beta = self._restored_pair(plexus)
        _add(beta, "beta_note", "project")
        plexus.enter(beta)
        assert memory.persist_memory("beta") == 0

        plexus.enter(alpha)
        assert memory.read_restore_baseline() == {}
        _add(alpha, "alpha_note", "project")
        assert memory.persist_memory("alpha", allow_unbaselined_delete=True) == 0
        assert plexus.published(f"{plexus.project_rel}/beta_note.md") is not None

    def test_the_copy_is_upstreams_version_and_every_uncopied_path_says_why(
        self, plexus
    ):
        from work_repo.git_ops import ConflictedPath

        alpha = plexus.enter(plexus.start("alpha"))
        scope = plexus.project_rel
        written, uncopied = memory._write_conflict_copies(
            [
                ConflictedPath(f"{scope}/edited.md", "upstream\n", "mine\n"),
                ConflictedPath(f"{scope}/gone_upstream.md", None, "mine\n"),
                ConflictedPath(f"{scope}/gone_here.md", "upstream\n", None),
                ConflictedPath(f"{scope}/{memory.MEMORY_INDEX}", "a\n", "b\n"),
                ConflictedPath("elsewhere/log.md", "a\n", "b\n"),
            ],
            [scope],
        )
        assert written == [f"edited.conflict-{alpha.sid}.md"]
        assert (alpha.memory_dir / written[0]).read_text() == "upstream\n"
        reasons = dict(uncopied)
        assert reasons[f"{scope}/gone_upstream.md"] == "deleted upstream, edited by this session"
        assert reasons[f"{scope}/gone_here.md"] == "deleted by this session, edited upstream"
        assert f"{scope}/{memory.MEMORY_INDEX}" in reasons
        assert reasons["elsewhere/log.md"] == "not a memory"

        # A second conflict never overwrites an unreconciled copy.
        again, _ = memory._write_conflict_copies(
            [ConflictedPath(f"{scope}/edited.md", "newer\n", "mine\n")], [scope]
        )
        assert again == [f"edited.conflict-{alpha.sid}-2.md"]
        assert (alpha.memory_dir / written[0]).read_text() == "upstream\n"

    def test_only_conflict_copy_names_are_excluded_from_memories(self):
        assert memory._is_conflict_copy(Path(f"note.conflict-{'a' * 32}.md"))
        assert memory._is_conflict_copy(Path("note.conflict-20261007-101500-4242-11111-2.md"))
        assert not memory._is_conflict_copy(Path("merge.conflict-notes.md"))
        assert not memory._is_conflict_copy(Path(f"sub/note.conflict-{'a' * 32}.md"))


class TestGapCNoBaselineRefusal:
    @staticmethod
    def _unbaselined(plexus) -> Session:
        """A session that never restored: no manifest, a memory dir holding
        only its own notes — one per scope the form has, so every scope is
        written and every upstream file is a local absence."""
        alpha = plexus.enter(plexus.start("alpha"))
        assert not alpha.manifest.exists()
        _add(alpha, "alpha_note", "project")
        if plexus.instance_rel is not None:
            _add(alpha, "alpha_pref", "user")
        return alpha

    def test_a_persist_without_a_baseline_deletes_nothing_and_names_the_absences(
        self, plexus, capsys
    ):
        alpha = self._unbaselined(plexus)
        assert memory.persist_memory("alpha") == 0

        for rel in plexus.fixture_rels():
            assert (alpha.clone / rel).is_file(), rel
            assert plexus.published(rel) is not None, rel
        assert plexus.published(f"{plexus.project_rel}/alpha_note.md") is not None

        out, err = capsys.readouterr()
        output = out + err
        assert PROJECT_MEMORY_FILE in output, output
        assert USER_MEMORY_FILE in output, output
        # This persist records a baseline, so a follow-up run with the flag
        # would delete none of these: the hint must not advertise it, and
        # names the explicit forget where the form has one.
        assert "--allow-unbaselined-delete" not in output, output
        if plexus.form == SCHEMA2:
            assert "work memory forget" in output, output

    def test_allow_unbaselined_delete_deletes_exactly_the_named_set(
        self, plexus, monkeypatch, capsys
    ):
        alpha = self._unbaselined(plexus)
        monkeypatch.setattr(
            sys, "argv",
            ["work", "memory", "persist", "--allow-unbaselined-delete", "-m", "alpha"],
        )
        refusal = None
        rc = None
        try:
            rc = cli.main()
        except SystemExit as exc:
            refusal = f"exit {exc.code}: {capsys.readouterr().err.strip()}"
        assert refusal is None, refusal
        assert rc == 0

        for rel in plexus.fixture_rels():
            assert not (alpha.clone / rel).exists(), rel
            assert plexus.published(rel) is None, rel
        assert plexus.published(f"{plexus.project_rel}/alpha_note.md") is not None
        assert not _rebase_in_progress(alpha)

    def test_a_persist_without_a_baseline_records_one_for_the_next_persist(
        self, plexus
    ):
        """Decision (task 50): the manifest a baseline-less persist writes
        itself IS a baseline for the next persist — it records what this
        session now holds, so a later removal is a deletion it may carry."""
        alpha = self._unbaselined(plexus)
        assert memory.persist_memory("alpha") == 0
        manifest = memory.read_restore_manifest()
        assert memory.PROJECT_SCOPE in manifest, manifest
        assert "alpha_note.md" in manifest[memory.PROJECT_SCOPE]["files"]

        _remove(alpha, "alpha_note.md")
        assert memory.persist_memory("alpha again") == 0

        rel = f"{plexus.project_rel}/alpha_note.md"
        assert not (alpha.clone / rel).exists(), rel
        assert plexus.published(rel) is None, rel

    def test_persist_parser_accepts_the_unbaselined_delete_flag(self):
        parser = cli.create_parser()
        assert parser.parse_args(
            ["memory", "persist", "--allow-unbaselined-delete"]
        ).allow_unbaselined_delete is True
        assert parser.parse_args(
            ["memory", "persist"]
        ).allow_unbaselined_delete is False


class TestGapDDeletionReporting:
    def test_persist_output_lists_every_deleted_path_with_the_count(
        self, schema2_plexus, capsys
    ):
        plexus = schema2_plexus
        alpha = plexus.enter(plexus.start("alpha"))
        assert memory.restore_memory() == 0

        # One implicit deletion: restored, then removed from the agent's dir.
        _remove(alpha, PROJECT_MEMORY_FILE)
        # One retirement-enforced deletion: an explicit forget, then an old
        # writer's stale copy reappearing at the retired path (:1744).
        instance = plexus.scope(alpha, memory.INSTANCE_SCOPE)
        stale = (instance / USER_MEMORY_FILE).read_text()
        memory.forget_memory(f"{plexus.instance_rel}/{USER_MEMORY_FILE}")
        (instance / USER_MEMORY_FILE).write_text(stale)
        capsys.readouterr()

        assert memory.persist_memory("alpha") == 0
        assert not (plexus.scope(alpha) / PROJECT_MEMORY_FILE).exists()
        assert not (instance / USER_MEMORY_FILE).exists()

        out, err = capsys.readouterr()
        output = out + err
        reports = [line for line in output.splitlines() if "delet" in line.lower()]
        assert any(PROJECT_MEMORY_FILE in line for line in reports), output
        retired = [line for line in reports if USER_MEMORY_FILE in line]
        assert retired and any("retire" in line.lower() for line in retired), output
        assert any("2" in line for line in reports), output


class TestGapEIndexReconciliation:
    HEADER = "# Memory\n\nNotes kept by hand.\n"

    def _seed_inconsistent_index(self, plexus) -> str:
        """Another writer leaves the project scope with a hand-written heading
        and commentary, a tombstone from ``work memory forget``, a dangling
        entry and a live file with no entry. Returns the tombstone line."""
        rel = plexus.project_rel

        def edit(clone: Path) -> None:
            scope = clone / rel
            (scope / "retired.md").write_text(_memory_file("retired", "project"))
            (scope / "orphan.md").write_text(_memory_file("orphan", "project"))
            index = scope / memory.MEMORY_INDEX
            index.write_text(
                self.HEADER
                + index.read_text()
                + _index_line("Retired", "retired.md")
                + _index_line("Ghost", "ghost.md")
            )
            memory.forget_memory(f"{rel}/retired.md", root=clone)

        plexus.publish(edit, "hand edits and a forget")
        published = plexus.published(f"{rel}/{memory.MEMORY_INDEX}")
        return next(
            line for line in published.splitlines(keepends=True) if "_archive/" in line
        )

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="#369 gap (e): entry lines are never reconciled with live files — "
        "a dangling entry survives and an unindexed live file stays unindexed",
    )
    def test_after_persist_entries_and_live_files_agree_and_hand_written_lines_stay(
        self, schema2_plexus
    ):
        plexus = schema2_plexus
        tombstone = self._seed_inconsistent_index(plexus)
        alpha = plexus.enter(plexus.start("alpha"))
        assert memory.restore_memory() == 0
        _add(alpha, "alpha_note", "project")
        assert memory.persist_memory("alpha") == 0

        project = plexus.scope(alpha)
        index = (project / memory.MEMORY_INDEX).read_text()
        assert index.startswith(self.HEADER), index
        assert tombstone in index, index
        assert index.index(tombstone) > index.index(f"({PROJECT_MEMORY_FILE})"), index
        assert "(ghost.md)" not in index, index
        for scope in (memory.PROJECT_SCOPE, memory.INSTANCE_SCOPE):
            directory = plexus.scope(alpha, scope)
            assert _entries(directory) == _live(directory), scope

        assert memory.persist_memory("alpha again") == 0
        assert (project / memory.MEMORY_INDEX).read_text() == index

        codes = {finding.code for finding in validate(alpha.clone).findings}
        assert not codes & {ENTRY_WITHOUT_FILE, FILE_WITHOUT_ENTRY}, codes

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="#369 gap (e): a session with no MEMORY.md leaves the scope's "
        "index untouched, so its new file is never indexed",
    )
    def test_a_session_with_no_index_still_gets_its_new_file_an_entry(self, schema2_plexus):
        plexus = schema2_plexus
        alpha = plexus.enter(plexus.start("alpha"))
        assert memory.restore_memory() == 0
        (alpha.memory_dir / memory.MEMORY_INDEX).unlink()
        (alpha.memory_dir / "alpha_note.md").write_text(_memory_file("alpha_note", "project"))
        assert memory.persist_memory("alpha") == 0

        project = plexus.scope(alpha)
        index = (project / memory.MEMORY_INDEX).read_text()
        assert "(alpha_note.md)" in index, index
        assert f"({PROJECT_MEMORY_FILE})" in index, index
        assert _entries(project) == _live(project)
        codes = {finding.code for finding in validate(alpha.clone).findings}
        assert FILE_WITHOUT_ENTRY not in codes, codes
