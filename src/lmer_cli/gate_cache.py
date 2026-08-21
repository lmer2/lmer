"""
Result cache for the gate's test suite: "has this exact tree already passed?"
(issue #269).

The 0.7.0 release ran the full suite FIVE times on one unchanged tree — the
scrub MR's push, the `main` push, the GitHub tag push, the publish gate, and
the GitLab tag push — three of them minutes apart on identical code. Nothing
about those runs differed except the ref being pushed, and a ref moves no
code, so each re-run bought a fresh copy of a verdict already held.

This module is the memory that removes them. A passing suite is recorded
under a key; a later gate that composes the same key reuses the pass instead
of re-running.

The constraint the issue states is the whole design:

    a release push must still be backed by a full green suite.

So a hit is legitimate ONLY because the identical tree was already proven
green *in this environment* — never as a way to publish untested code:

- what the key covers, exactly: the committed tree
  (``git rev-parse HEAD^{tree}``), a digest of everything uncommitted
  (:func:`working_tree_digest`), the exact test invocation, the interpreter
  that will run it (resolved path plus its own version string), and the
  dependency surface around that interpreter (:func:`environment_identity`
  — the venv marker plus the distributions installed beside it). The
  invocation is what keeps the text-diff subset run
  (:data:`gates.TEXT_DIFF_SCOPE`) from ever satisfying a full-suite need —
  different argv, different key;
- the environment that invocation is handed, minus
  :data:`VOLATILE_ENV_NAMES`, is checked too, but from the ENTRY rather than
  from the filename (:func:`read_pass`, the shape ``tree``/``working``
  already use). A difference is still a miss — the safety property is
  unchanged — but a miss that can be NAMED: the entry carries one digest of
  the whole environment plus a per-variable digest, so
  :func:`environment_mismatch` can answer "which variable moved?" instead of
  leaving a cache that mysteriously never hits. Keying the filename on the
  environment instead did exactly that: ``~/.bashrc``'s mise activation
  exports a fresh ``__MISE_SESSION`` per login shell, so every gate composed
  a unique key, hit nothing, and said nothing about why;
- only PASSES are recorded. A failing suite is never cached: flaky failures
  would stick, and re-running a failure costs nothing anyone minds paying;
- anything unknown means "run the suite". Not a git repo, a git command that
  fails, a file that cannot be hashed, a working tree the porcelain parser
  does not recognize, a cache directory this uid does not own — every one of
  them answers None, and None is never "no change";
- entries live in ``/tmp`` (:data:`DEFAULT_CACHE_DIR`) on purpose. A verdict
  is about a tree AND the environment that ran it; a cache that outlived the
  container would let a pass earned under one set of installed dependencies
  answer for another. :data:`MAX_ENTRY_AGE_SECONDS` bounds it further.

What remains OUTSIDE the key, stated plainly, because a cache is only as
honest as its boundary:

- anything a package changes without changing its ``.dist-info`` name — an
  editable install whose source moved, a rebuilt wheel of the same version,
  a distribution installed somewhere the interpreter's own site directories
  do not name;
- the variables in :data:`VOLATILE_ENV_NAMES`;
- the machine itself: kernel, libc, the C libraries an extension module
  links against, the clock, the network, and anything else a test reads from
  the world rather than from the tree. The ``/tmp`` default and the one-week
  horizon are the bounds on that, and ``LMER_GATE_NO_CACHE=1`` is the answer
  when one of them moved under an unchanged tree.

No environment VALUE is ever written to an entry or printed: the environment
routinely carries credentials. What an entry holds is digests — one over the
whole environment and one per variable name — and what a miss prints is
variable names.

Everything here fails soft: no cache problem may change a gate's exit code
(the same contract as receipt emission in ``bin/gate-check``).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, List, Mapping, Optional, Sequence,
                    Tuple)

from .gate_lock import format_age
from .util import get_bool_env

#: Where recorded passes live. ``/tmp`` is deliberate — see the module
#: docstring: a verdict describes a tree *in this environment*, and the cache
#: must not outlive it.
DEFAULT_CACHE_DIR = "/tmp/lmer-gate-cache"

#: Override for the cache directory. Read at call time, never at import, so a
#: test can point it somewhere else after this module is imported (the same
#: reason as ``gate_lock.LOCK_DIR_ENV``).
CACHE_DIR_ENV = "LMER_GATE_CACHE_DIR"

#: Kill switch (``get_bool_env`` semantics; unset or falsy keeps the cache on).
#: Truthy means every gate re-runs the suite and records nothing — the escape
#: hatch for the one thing the key cannot see, a changed environment around an
#: unchanged tree.
DISABLE_ENV = "LMER_GATE_NO_CACHE"

#: Bumped whenever WHAT is hashed changes. Part of the key, so old entries
#: stop being found rather than being read under new rules.
CACHE_FORMAT_VERSION = 3

#: One JSON file per key.
ENTRY_SUFFIX = ".json"

#: Owner-only, both of them. An entry authorizes SKIPPING the test suite, so
#: whoever can write one can mint a passing gate and a receipt indistinguishable
#: from an earned one — and the default directory lives in a world-writable
#: ``/tmp``. Same modes and the same reasoning as the platform state tree
#: (``lmer_platform.store.STATE_DIR_MODE``/``SNAPSHOT_FILE_MODE``).
CACHE_DIR_MODE = 0o700
CACHE_ENTRY_MODE = 0o600

#: The only environment variables left out of the environment digest (see
#: :func:`cache_environment`). Everything else the run is handed is hashed.
#:
#: A DENYLIST, not an allowlist of "variables that can reach pytest", because
#: of which way each one fails. An allowlist is unsafe by default: every
#: variable nobody thought of is silently excluded, and an excluded variable
#: that does matter produces a false hit — a verdict handed to a run that
#: would not have behaved the same way. A denylist inverts that. Being unsafe
#: requires someone to explicitly name a variable as inert, which is a small
#: reviewable set, and the cost of a MISSING entry is a cache that does not
#: hit: expensive, never wrong.
#:
#: None of these can reach the suite's behaviour, and all of them churn
#: between invocations: ``_`` is the shell's last-argument variable (a
#: different value for every command typed), ``SHLVL`` counts nested shells,
#: ``OLDPWD`` remembers where a ``cd`` came from, and ``__MISE_SESSION`` is
#: the token mise's shell activation mints per interactive shell — it names a
#: shell session, is read by nothing but mise's own hooks, and made the cache
#: inert across login shells while it was in the key. ``PWD`` is deliberately
#: NOT here — the directory a suite runs in is part of the run.
#:
#: The entry-side environment check (:func:`environment_mismatch`) is what
#: keeps a wrong prediction here visible: a variable this list should name but
#: does not costs suite runs while saying, on every one of them, which
#: variable is doing it.
VOLATILE_ENV_NAMES = frozenset({"_", "SHLVL", "OLDPWD", "__MISE_SESSION"})

#: How long a recorded pass may be reused, and the horizon the opportunistic
#: prune sweeps. A week is far past any real "did this tree already pass?"
#: window while keeping the directory from growing without limit.
MAX_ENTRY_AGE_SECONDS = 7 * 24 * 60 * 60

#: The working-tree digest of a clean tree. A literal marker, not an empty
#: string: "nothing is uncommitted" is a FACT about the tree and has to be
#: distinguishable from "the digest could not be computed", which is None.
CLEAN_TREE = "clean"

#: Stand-in blob hash for a path the porcelain reports but the filesystem no
#: longer has (a deletion). There is no content to hash, and the deletion
#: itself is what the key has to capture.
DELETED_BLOB = "deleted"

#: The only outcome ever recorded. Present in the entry so a reader (and
#: :func:`parse_entry`) never has to infer it from the file's existence.
PASS_OUTCOME = "pass"

#: How many differing variable names a miss notice prints before summarizing
#: the rest. Two shells differ in one variable; two genuinely different
#: environments can differ in dozens, and a diagnostic nobody reads to the end
#: is not a diagnostic.
MISMATCH_NAME_LIMIT = 8

#: How the interpreter that will run the suite is asked what it is. Its own
#: report, not the gate's ``sys.version``: the gate runs under the lmer venv
#: while the suite may run under the project's, and it is the latter's verdict
#: being cached.
VERSION_PROBE = "import sys; print(sys.version)"

#: How that same interpreter is asked where its packages live — its prefix (the
#: venv marker) and every site directory it imports from. Only the paths are
#: asked for; the listing itself is done here, so what lands in the key comes
#: from one place. JSON so a path containing a newline cannot be read as two.
ENVIRONMENT_PROBE = (
    "import json,site,sys,sysconfig;"
    "sites=[sysconfig.get_paths()['purelib']];"
    "sites+=list(getattr(site,'getsitepackages',lambda: [])());"
    "sites.append(getattr(site,'getusersitepackages',lambda: '')());"
    "print(json.dumps({'prefix':sys.prefix,'sites':sites}))"
)

#: Suffixes naming one installed distribution in a site directory. Listing them
#: is a directory read, not a ``pip list`` subprocess, and it catches the drift
#: the key would otherwise miss entirely: an image rebuilt with different
#: dependencies under an unchanged tree and an unchanged Python version.
DISTRIBUTION_SUFFIXES = (".dist-info", ".egg-info")

#: ``(command, check) -> (returncode, stdout, stderr)`` — injected by the
#: caller (``GateSystem.run_command``), so this module shells out to nothing
#: itself and the unit tests can drive it with a real git repo or a stub.
Runner = Callable[..., Tuple[int, str, str]]


@dataclass(frozen=True)
class Fingerprint:
    """The cache key, and everything an entry found under it is checked against.

    ``tree`` and ``working`` are also the two facts a hit notice states.
    ``environment`` is the digest of the environment the run will be handed
    and ``environment_hashes`` is the same environment one digest per variable
    name — the second exists only so a mismatch can be NAMED
    (:func:`environment_mismatch`). Neither ever holds a value.
    """
    key: str
    tree: str
    working: str
    environment: str = ""
    environment_hashes: Dict[str, str] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.working == CLEAN_TREE


# ---------------------------------------------------------------------------
# Pure logic — unit-testable seams. No env reads, no filesystem, no
# subprocesses; every input is injected by the caller (gate_lock's shape).
# ---------------------------------------------------------------------------


def _digest(payload: Any) -> str:
    """SHA-256 over a canonical JSON rendering of *payload*.

    JSON rather than a joined string: the fields include arbitrary file paths
    and an argv, and any separator character chosen for them can also occur
    inside them. Canonical form (sorted keys, no incidental whitespace) is
    what makes the digest reproducible across processes.
    """
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_status(text: str) -> Optional[List[Tuple[str, str, Optional[str]]]]:
    """``(status, path, origin)`` per entry of ``git status --porcelain -z``.

    ``-z`` because it is NUL-separated AND unquoted: a non-ASCII path arrives
    as the bytes git tracks instead of a C-quoted ``"caf\\303\\251.md"``, so
    the same file digests the same way whatever it is called (the reasoning
    ``gates._changed_paths`` already applies to its own probes).

    A rename or copy occupies TWO records — the destination, then the source —
    and the source is kept as *origin* rather than dropped, or ``A -> C`` and
    ``B -> C`` would digest identically.

    None means the output is not something this parser recognizes, which the
    caller reads as "unknown" and therefore as "run the suite". Fail closed:
    guessing at an unparsed record is how a cache starts answering for a tree
    it never saw.
    """
    if not isinstance(text, str):
        return None
    records = [record for record in text.split("\0") if record]
    entries: List[Tuple[str, str, Optional[str]]] = []
    index = 0
    while index < len(records):
        record = records[index]
        # `XY <path>`: two status characters and the separating space.
        if len(record) < 4 or record[2] != " ":
            return None
        status, path = record[:2], record[3:]
        origin = None
        if "R" in status or "C" in status:
            if index + 1 >= len(records):
                return None  # a rename whose source record never arrived
            origin = records[index + 1]
            index += 1
        entries.append((status, path, origin))
        index += 1
    return entries


def compose_working_digest(
        entries: Sequence[Tuple[str, str, Optional[str]]],
        blobs: Sequence[Tuple[str, str]]) -> str:
    """Digest of everything uncommitted, or :data:`CLEAN_TREE` when nothing is.

    The committed tree hash alone is wrong the moment anything is uncommitted:
    the working tree is what pytest imports. Both halves are hashed — the
    porcelain status (which says WHAT kind of change each path carries, so a
    staged edit and the same edit unstaged are different states) and the blob
    hash of each path's current content (which says what the change IS).
    """
    if not entries and not blobs:
        return CLEAN_TREE
    return _digest({
        "entries": [list(entry) for entry in entries],
        "blobs": dict(blobs),
    })


def cache_environment(environment: Mapping[str, str]) -> Dict[str, str]:
    """*environment* as the cache should carry it: everything but the volatile few.

    The run inherits the caller's whole environment, so the whole environment
    is what decides how it behaves — this repo's own suite skips integration
    tests on ambient state (``tests/_lmer_runtime.py``), asserts on ambient
    ``GIT_CONFIG_*`` (#264), and pytest itself reads ``PYTEST_ADDOPTS``, which
    can narrow a run from outside the argv the cache records. Naming the
    variables that matter would mean maintaining that list against every test
    anyone writes; taking all of them and naming only the exclusions
    (:data:`VOLATILE_ENV_NAMES` — see there for why that direction) fails the
    safe way when the list is wrong.
    """
    return {name: value for name, value in environment.items()
            if name not in VOLATILE_ENV_NAMES}


def hash_environment(
        environment: Mapping[str, str]) -> Optional[Dict[str, str]]:
    """``{name: digest of its value}`` for every variable that is kept, or None.

    Filters through :func:`cache_environment` itself — the filter is
    idempotent, and a caller that forgot it would otherwise put a volatile
    variable back into the comparison.

    A digest per NAME, not the values: this map is written to an entry, and
    the environment routinely carries credentials. The name is hashed
    alongside its value so an entry cannot even show that two variables hold
    the same one.

    None when anything in *environment* is not a string, the way every other
    unknown here answers: a mapping this cannot digest exactly is one no key
    may be composed from (:func:`compose_key`'s reasoning, for the half that
    no longer lives in the key).
    """
    kept = cache_environment(environment)
    if not all(isinstance(name, str) and isinstance(value, str)
               for name, value in kept.items()):
        return None
    return {name: _digest({"name": name, "value": value})
            for name, value in kept.items()}


def compose_environment_digest(hashes: Mapping[str, str]) -> str:
    """One digest over :func:`hash_environment`'s map — what a lookup compares.

    Separate from the map so the comparison in :func:`read_pass` is a single
    string equality, exactly like ``tree`` and ``working``; the map is only
    consulted once that equality has already failed.
    """
    return _digest({"environment": dict(hashes)})


def differing_names(recorded: Mapping[str, str],
                    current: Mapping[str, str]) -> List[str]:
    """The variable NAMES whose digests differ between two environments.

    Added, removed and changed all count, and the answer is sorted so two
    runs describing the same difference describe it the same way. Names only:
    the caller prints this.
    """
    names = set(recorded) | set(current)
    return sorted(name for name in names
                  if recorded.get(name) != current.get(name))


def environment_miss_reason(names: Sequence[str]) -> Optional[str]:
    """Structured reason for a nameable environment miss, or ``None``.

    The point of the line: a cache that never hits is otherwise silent, which
    is how keying the filename on a per-shell variable survived a review round
    and a measurement. Bounded by :data:`MISMATCH_NAME_LIMIT`, and never
    carrying a value.
    """
    if not names:
        return None
    shown = list(names[:MISMATCH_NAME_LIMIT])
    listed = ", ".join(shown)
    remaining = len(names) - len(shown)
    if remaining > 0:
        listed += f", +{remaining} more"
    return ("same tree and invocation, environment differs "
            f"({listed})")


def describe_miss(names: Sequence[str]) -> Optional[str]:
    """The terminal line for a nameable environment miss, or ``None``."""
    reason = environment_miss_reason(names)
    return f"Cache miss: {reason}" if reason else None


def compose_key(tree: Optional[str], working: Optional[str],
                argv: Optional[Sequence[str]],
                interpreter: Optional[Sequence[str]],
                dependencies: Optional[str] = None,
                version: int = CACHE_FORMAT_VERSION) -> Optional[str]:
    """The cache key, or None when any input is unknown.

    Every input is one of the things that could change the verdict:

    - *tree* — ``git rev-parse HEAD^{tree}``, the committed content;
    - *working* — :func:`compose_working_digest`, everything uncommitted;
    - *argv* — the exact command the suite will run under. This is what lets
      the text-diff subset (#269 MR A) and this cache coexist: a subset run
      composes a DIFFERENT key from a full-suite run, so a partial pass can
      never be handed back to a caller that needs the whole suite;
    - *interpreter* — ``(resolved path, its own version string)``;
    - *dependencies* — :func:`environment_identity`, what is installed around
      the interpreter. The argv and the import path say which code runs; this
      says what it imports;
    - *version* — :data:`CACHE_FORMAT_VERSION`, so changing what is hashed
      makes old entries unfindable rather than misread.

    The environment is deliberately NOT here: it is checked, but out of the
    entry (:func:`read_pass`). In the filename it made every login shell
    compose its own key — a cache that never hit and never said so — and it
    left the directory accumulating one entry per shell.

    None in, None out, deliberately: a key composed from a missing input
    would be a key that two different trees can share.
    """
    if not tree or not working or not argv or not interpreter:
        return None
    if not dependencies:
        return None
    if not all(isinstance(part, str) and part for part in argv):
        return None
    if not all(isinstance(part, str) and part for part in interpreter):
        return None
    return _digest({
        "version": version,
        "tree": tree,
        "working": working,
        "argv": list(argv),
        "interpreter": list(interpreter),
        "dependencies": dependencies,
    })


def parse_entry(text: str) -> Optional[Dict[str, Any]]:
    """Normalize one entry file's contents, or None when it is unusable.

    Rejected: a torn or non-JSON write, a document that is not an object, an
    entry written by another cache format, anything whose outcome is not a
    pass, and anything without a readable timestamp — the age is what bounds
    reuse, so an entry that cannot be dated cannot be trusted to be current.
    ``summary`` and ``gate`` are decoration for the notice; a missing one
    still counts as a hit.

    ``tree``, ``working`` and ``environment`` are carried through as-is (None
    when absent) for :func:`read_pass` to check against the fingerprint that
    found the file — without those checks the only thing authorizing the skip
    is a filename. ``environment_hashes`` is decoration for the miss notice,
    so an entry without a usable one still reads (as an empty map) rather
    than being thrown away.
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("version") != CACHE_FORMAT_VERSION:
        return None
    if parsed.get("outcome") != PASS_OUTCOME:
        return None
    try:
        created_at = float(parsed.get("created_at"))
    except (TypeError, ValueError):
        return None
    argv = parsed.get("argv")
    summary = parsed.get("summary")
    gate = parsed.get("gate")
    hashes = parsed.get("environment_hashes")
    return {
        "outcome": PASS_OUTCOME,
        "created_at": created_at,
        "argv": [a for a in argv if isinstance(a, str)] if isinstance(argv, list) else [],
        "summary": summary.strip() if isinstance(summary, str) and summary.strip() else None,
        "gate": gate if isinstance(gate, str) and gate.strip() else "an earlier gate",
        "tree": parsed.get("tree") if isinstance(parsed.get("tree"), str) else None,
        "working": (parsed.get("working")
                    if isinstance(parsed.get("working"), str) else None),
        "environment": (parsed.get("environment")
                        if isinstance(parsed.get("environment"), str) else None),
        "environment_hashes": (
            {name: value for name, value in hashes.items()
             if isinstance(name, str) and isinstance(value, str)}
            if isinstance(hashes, dict) else {}),
    }


def entry_is_current(entry: Dict[str, Any], now: float,
                     max_age: float = MAX_ENTRY_AGE_SECONDS) -> bool:
    """Whether *entry* is young enough to still answer for its tree.

    A future timestamp (a clock step, a file copied from elsewhere) is not
    current either: it would otherwise never expire.
    """
    age = now - entry["created_at"]
    return 0 <= age <= max_age


def describe_hit(entry: Dict[str, Any], fingerprint: Fingerprint,
                 now: Optional[float] = None) -> List[str]:
    """The lines a cache hit prints: when, by what, over what, and how to opt out.

    A skipped suite that does not say what it is standing on is indistinguishable
    from a suite nobody ran, so this names all four.
    """
    elapsed = (now if now is not None else time.time()) - entry["created_at"]
    age = format_age(elapsed) or "moments"
    proven = f"Proven green {age} ago by {entry['gate']}"
    if entry.get("summary"):
        proven += f" ({entry['summary']})"
    state = ("working tree clean" if fingerprint.clean
             else f"working tree dirty ({fingerprint.working[:12]}…)")
    return [
        proven,
        f"Tree {fingerprint.tree[:12]}…, {state}, same test invocation, "
        "interpreter and environment.",
        f"{DISABLE_ENV}=1 forces a re-run.",
    ]


# ---------------------------------------------------------------------------
# Impure helpers — every one answers None ("unknown", i.e. run the suite)
# rather than raising, and none of them can change a gate's exit code.
# ---------------------------------------------------------------------------


def cache_dir() -> Path:
    """The entry directory, honoring :data:`CACHE_DIR_ENV` at call time."""
    return Path(os.environ.get(CACHE_DIR_ENV, "").strip() or DEFAULT_CACHE_DIR)


def safe_cache_dir(create: bool = False) -> Optional[Path]:
    """The cache directory when this uid can trust it, else None.

    None is a miss, never an error: every caller treats "no directory" the way
    it treats an absent entry, so a refusal here costs a suite run and cannot
    break a gate.

    The default directory lives in a world-writable ``/tmp``, and an entry in
    it authorizes skipping the suite — so it is refused unless this uid owns
    it, and a symlink is refused outright rather than followed to wherever it
    points. A directory already there with looser bits is tightened rather
    than adopted as found: ``mkdir`` does nothing to a directory that exists,
    so without that the hardening would apply only where it is not needed
    (:func:`lmer_platform.store.ensure_state_dir` makes the same correction
    for the same reason). Only bits outside :data:`CACHE_DIR_MODE` trigger the
    ``chmod``, so nothing is ever widened.

    With the directory owner-only, the entries inside it can only be written
    by this uid or by root, and root needs no cache entry to forge a gate.
    """
    directory = cache_dir()
    try:
        if directory.is_symlink():
            return None
        if not directory.is_dir():
            if not create:
                return None
            directory.mkdir(mode=CACHE_DIR_MODE, parents=True, exist_ok=True)
            # Behind the mkdir, which a umask can only have narrowed further.
            directory.chmod(CACHE_DIR_MODE)
            return directory
        info = directory.stat()
        if info.st_uid != os.geteuid():
            return None
        if stat.S_IMODE(info.st_mode) & ~CACHE_DIR_MODE:
            directory.chmod(CACHE_DIR_MODE)
        return directory
    except OSError:
        return None


def _open_no_follow(path: str, flags: int) -> int:
    """``open`` opener that refuses a symlink at the final component.

    The directory mode is the real defence (:func:`safe_cache_dir`); this is
    the cheap second one, for the window in which a directory that used to be
    somebody else's still holds their names.
    """
    return os.open(path, flags | os.O_NOFOLLOW)


def cache_enabled() -> bool:
    """False when :data:`DISABLE_ENV` is truthy (default: enabled)."""
    return not get_bool_env(DISABLE_ENV)


def entry_path(key: str, directory: Optional[Path] = None) -> Path:
    return (directory or cache_dir()) / f"{key}{ENTRY_SUFFIX}"


def _command_output(run: Runner, command: List[str]) -> Optional[str]:
    """stdout of *command*, or None when it failed or answered no text.

    The runner is injected, so the isinstance check is not paranoia about
    git: it is what keeps a stub (or a mocked ``subprocess.run``) handing
    back a non-string from being hashed as if it were a tree.
    """
    try:
        code, stdout, _ = run(command, check=False)
    except Exception:
        # No cache problem may change a gate's verdict — the receipt contract,
        # applied to a callable this module does not own.
        return None
    if code != 0 or not isinstance(stdout, str):
        return None
    return stdout


def committed_tree(run: Runner) -> Optional[str]:
    """``HEAD^{tree}`` — the committed content, or None outside a git repo."""
    stdout = _command_output(run, ["git", "rev-parse", "HEAD^{tree}"])
    if stdout is None:
        return None
    tree = stdout.strip()
    return tree or None


def indexed_tree(run: Runner) -> Optional[str]:
    """The tree object represented by the current index, or ``None``.

    ``git write-tree`` does not change staged content or working-tree files, but
    it may rewrite the index's cache-tree extension, takes the index lock, and
    writes tree objects. It is therefore reserved for a cache-enabled commit
    gate that can consume the identity after committing. Equality with
    ``HEAD^{tree}`` then proves the clean post-commit tree is exactly the staged
    content whose tests passed.
    """
    stdout = _command_output(run, ["git", "write-tree"])
    if stdout is None:
        return None
    tree = stdout.strip()
    return tree or None


def repo_toplevel(run: Runner) -> Optional[Path]:
    """The root of the repository the runner is talking to, or None.

    Everything ``git status --porcelain`` reports is relative to THIS
    directory, whatever the runner's cwd is, so it is the only thing those
    paths may be resolved against.
    """
    stdout = _command_output(run, ["git", "rev-parse", "--show-toplevel"])
    if stdout is None:
        return None
    toplevel = stdout.strip()
    return Path(toplevel) if toplevel else None


def working_tree_digest(run: Runner) -> Optional[str]:
    """Digest of everything uncommitted, :data:`CLEAN_TREE`, or None.

    Anchored at :func:`repo_toplevel` rather than at the caller's cwd, and
    None when the toplevel cannot be resolved. Porcelain paths are
    repo-root-relative no matter where the command ran, so resolving them
    against a subdirectory finds nothing on disk — every dirty path then looks
    deleted, no content is ever hashed, and the digest stops depending on what
    the files contain. ``hash-object`` is anchored the same way (``git -C``),
    because it reads its arguments relative to ITS cwd.

    The status flags are pinned rather than inherited. ``--untracked-files=all``:
    without it ``status.showUntrackedFiles=no`` in the repo's or the user's
    config makes the porcelain silent about untracked files, and a tree
    carrying an untracked ``conftest.py`` that deselects the suite digests
    byte-identically to a clean one — a config that turns the cache into a
    forger. It also enumerates untracked directories file-by-file, so a file
    added inside one moves the digest instead of hiding behind a summary line.
    ``--ignore-submodules=none``: same reasoning, for a submodule whose config
    asks for its dirtiness to be hidden. (:func:`gates._changed_paths` reaches
    for the config-immune ``git ls-files --others --exclude-standard`` for
    exactly this reason.)

    None otherwise for every state this cannot pin down exactly: a status
    output the parser does not recognize, a reported path that is a DIRECTORY
    on disk (a dirty submodule is the one git still reports this way, and a
    directory's content is not something ``hash-object`` states in one line),
    or a ``hash-object`` call that fails — including one refused for being too
    long, which a very large dirty tree can produce.
    """
    toplevel = repo_toplevel(run)
    if toplevel is None:
        return None
    stdout = _command_output(run, [
        "git", "status", "--porcelain", "-z",
        "--untracked-files=all", "--ignore-submodules=none"])
    if stdout is None:
        return None
    entries = parse_status(stdout)
    if entries is None:
        return None

    blobs: List[Tuple[str, str]] = []
    to_hash: List[str] = []
    for _status, path, _origin in entries:
        target = toplevel / path
        if target.is_symlink() or target.is_file():
            to_hash.append(path)
        elif target.exists():
            return None
        else:
            blobs.append((path, DELETED_BLOB))

    if to_hash:
        hashed = _command_output(
            run, ["git", "-C", str(toplevel), "hash-object", "--"] + to_hash)
        if hashed is None:
            return None
        values = hashed.split()
        if len(values) != len(to_hash):
            return None
        blobs.extend(zip(to_hash, values))

    return compose_working_digest(entries, blobs)


def interpreter_identity(run: Runner,
                         python_cmd: str) -> Optional[Tuple[str, str]]:
    """``(resolved path, version string)`` of the interpreter running the suite.

    The path is resolved through PATH but NOT through symlinks: two venvs
    linking the same base interpreter are two different environments, and
    collapsing them would let one venv's pass answer for the other.
    """
    if not python_cmd:
        return None
    resolved = shutil.which(python_cmd)
    if not resolved:
        return None
    stdout = _command_output(run, [python_cmd, "-c", VERSION_PROBE])
    if stdout is None:
        return None
    version = " ".join(stdout.split())
    if not version:
        return None
    return resolved, version


def environment_identity(run: Runner, python_cmd: str) -> Optional[str]:
    """Digest of the dependency surface around the suite's interpreter, or None.

    The venv marker (``pyvenv.cfg``, which names the base interpreter and the
    tool that built it) plus the sorted names of the distributions installed
    in the site directories that interpreter imports from. A directory
    listing, not a ``pip`` subprocess, and it closes the drift the rest of the
    key cannot see: an image rebuilt with different dependencies leaves the
    tree, the argv, the import path and the Python version all unchanged.

    What it does not see is stated in the module docstring — a name is not a
    content hash, so a rebuilt wheel of the same version reads the same.

    A site directory that is not there is not a failure: interpreters name
    site directories they never populate (the user site of a venv that
    disables it), and "nothing installed there" is a fact. A directory that
    exists and cannot be read IS unknown, and answers None.
    """
    stdout = _command_output(run, [python_cmd, "-c", ENVIRONMENT_PROBE])
    if stdout is None:
        return None
    try:
        probe = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(probe, dict):
        return None
    prefix, sites = probe.get("prefix"), probe.get("sites")
    if not isinstance(prefix, str) or not prefix:
        return None
    if not isinstance(sites, list) or not all(isinstance(s, str) for s in sites):
        return None

    distributions = set()
    try:
        marker = Path(prefix) / "pyvenv.cfg"
        # A system interpreter has no marker; that is a fact about it, not an
        # unknown, and it stays distinguishable from an empty one.
        config = marker.read_text(encoding="utf-8") if marker.is_file() else None
        for site_dir in sites:
            directory = Path(site_dir) if site_dir else None
            if directory is None or not directory.is_dir():
                continue
            distributions.update(
                child.name for child in directory.iterdir()
                if child.name.endswith(DISTRIBUTION_SUFFIXES))
    except (OSError, UnicodeDecodeError):
        return None
    return _digest({"venv": config, "distributions": sorted(distributions)})


def compute_fingerprint(run: Runner, argv: Sequence[str],
                        environment: Optional[Dict[str, str]] = None
                        ) -> Optional[Fingerprint]:
    """The :class:`Fingerprint` for this tree, invocation and environment, or None.

    None whenever any input is unknown, and the caller must then neither read
    nor write: an unknown input is a tree this module cannot claim to have
    seen before. An absent *environment* is not unknown — it is an empty one,
    and it digests as such.

    The repository is the one *run* answers for (:func:`repo_toplevel`), not
    one the caller names: a caller-supplied root that disagreed with it would
    silently take the content out of the key.
    """
    if not argv:
        return None
    tree = committed_tree(run)
    if tree is None:
        return None
    working = working_tree_digest(run)
    if working is None:
        return None
    interpreter = interpreter_identity(run, argv[0])
    if interpreter is None:
        return None
    dependencies = environment_identity(run, argv[0])
    if dependencies is None:
        return None
    key = compose_key(tree, working, argv, interpreter, dependencies)
    if key is None:
        return None
    hashes = hash_environment(environment or {})
    if hashes is None:
        return None
    return Fingerprint(key=key, tree=tree, working=working,
                       environment=compose_environment_digest(hashes),
                       environment_hashes=hashes)


def _load_entry(fingerprint: Optional[Fingerprint],
                now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The usable entry filed under *fingerprint*'s key, environment aside.

    Everything :func:`read_pass` requires except the environment comparison,
    so that a miss on the environment alone can still reach the entry it
    missed against and name what differs (:func:`environment_mismatch`).
    """
    if fingerprint is None or not fingerprint.key or not cache_enabled():
        return None
    directory = safe_cache_dir()
    if directory is None:
        return None
    try:
        with open(entry_path(fingerprint.key, directory), "r",
                  encoding="utf-8", opener=_open_no_follow) as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    entry = parse_entry(text)
    if entry is None:
        return None
    if (entry["tree"] != fingerprint.tree
            or entry["working"] != fingerprint.working):
        return None
    if not entry_is_current(entry, now if now is not None else time.time()):
        return None
    return entry


def read_pass(fingerprint: Optional[Fingerprint],
              now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The recorded pass for *fingerprint*, or None — including for no fingerprint.

    An unreadable, torn, corrupt, foreign-format or expired entry all read as
    None: the cost of ignoring a usable entry is one suite run, and the cost
    of misreading one is a verdict nobody earned. So is an entry found in a
    directory this uid does not own (:func:`safe_cache_dir`).

    The whole fingerprint rather than its key, because the entry has to agree
    with what was looked up. Without that comparison the only thing
    authorizing the skip is a filename: an entry hand-written into the
    directory needs nothing but ``version``, ``outcome`` and ``created_at``
    to mint a pass. The same check catches a hash collision and an entry
    written under a :data:`CACHE_FORMAT_VERSION` that was later reverted.

    The environment is compared here rather than in the key, and on the same
    terms: an entry that answers for another environment is a MISS. What the
    move buys is not safety, which is unchanged, but the ability to say why
    (:func:`environment_mismatch`) — and an entry that is overwritten by the
    next environment instead of joining one file per shell in the directory.
    """
    entry = _load_entry(fingerprint, now)
    if entry is None:
        return None
    if entry["environment"] != fingerprint.environment:
        return None
    return entry


def environment_mismatch(fingerprint: Optional[Fingerprint],
                         now: Optional[float] = None) -> List[str]:
    """Which variables an otherwise-matching entry disagrees with, by name.

    Empty whenever there is nothing to say: no entry, an entry that does not
    answer for this tree, or one whose environment matches (in which case
    :func:`read_pass` returned it). Only worth calling after a miss, and it
    costs one small read of a file that was just read — against a suite run
    of minutes.

    Names, never values (:func:`hash_environment` is what is compared).
    """
    entry = _load_entry(fingerprint, now)
    if entry is None or fingerprint is None:
        return []
    if entry["environment"] == fingerprint.environment:
        return []
    return differing_names(entry["environment_hashes"],
                           fingerprint.environment_hashes)


def record_pass(fingerprint: Optional[Fingerprint], summary: Optional[str],
                argv: Sequence[str], gate: str,
                now: Optional[float] = None) -> Optional[Path]:
    """Record a passing suite for *fingerprint*; returns the file, or None.

    Only ever called for a pass (see the module docstring). Every filesystem
    step is swallowed — a cache that cannot be written costs a suite run,
    which is exactly the state before this module existed.

    ``tree``, ``working`` and the environment digest are recorded because
    :func:`read_pass` checks them against the fingerprint that finds the
    file; they are the entry stating what it answers for. The per-variable
    digests ride along for the miss notice — digests of the values, never the
    values, and the file is owner-only besides.

    Written to a temp with ``O_CREAT|O_EXCL|O_NOFOLLOW`` at
    :data:`CACHE_ENTRY_MODE` and renamed over the target, so the mode is on
    the inode before the first byte (never a world-readable window, the shape
    ``lmer_platform.store._create_owner_only`` uses) and a reader sees a whole
    entry or none. The temp carries the pid so two writers never share one,
    and a leading dot so a directory listing shows it for what it is; nothing
    reads it either way, since :func:`read_pass` opens the one exact filename
    its key composes, and :func:`prune_entries` — whose ``*.json`` glob does
    match a dotted name — expires a leftover like any other file.
    """
    if fingerprint is None or not cache_enabled():
        return None
    directory = safe_cache_dir(create=True)
    if directory is None:
        return None
    path = entry_path(fingerprint.key, directory)
    record = {
        "version": CACHE_FORMAT_VERSION,
        "outcome": PASS_OUTCOME,
        "created_at": now if now is not None else time.time(),
        "gate": gate,
        "summary": summary,
        "argv": list(argv),
        "tree": fingerprint.tree,
        "working": fingerprint.working,
        "environment": fingerprint.environment,
        "environment_hashes": dict(fingerprint.environment_hashes),
    }
    temp = directory / f".{fingerprint.key}.{os.getpid()}{ENTRY_SUFFIX}"
    try:
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        descriptor = os.open(
            str(temp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            CACHE_ENTRY_MODE)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        os.replace(temp, path)
    except (OSError, ValueError, TypeError):
        # Including a temp left by a crashed write of this same pid: removing
        # it is what keeps the O_EXCL from making the entry unwritable forever.
        try:
            temp.unlink()
        except OSError:
            pass
        return None
    # Opportunistic, like gate_lock's marker pruning: the writers are the only
    # processes guaranteed to visit this directory, so nobody has to remember
    # to sweep it.
    prune_entries(now)
    return path


def prune_entries(now: Optional[float] = None,
                  max_age: float = MAX_ENTRY_AGE_SECONDS) -> int:
    """Delete entries past *max_age*; returns how many went. Never raises.

    An entry that cannot be parsed is judged by its mtime rather than kept
    forever — junk in this directory must expire like everything else. A
    directory this uid does not own is not swept at all: it is not this
    process's to delete from (:func:`safe_cache_dir`).
    """
    directory = safe_cache_dir()
    if directory is None:
        return 0
    try:
        candidates = sorted(directory.glob(f"*{ENTRY_SUFFIX}"))
    except OSError:
        return 0
    moment = now if now is not None else time.time()
    removed = 0
    for candidate in candidates:
        try:
            entry = parse_entry(candidate.read_text(encoding="utf-8"))
            age = (moment - entry["created_at"] if entry is not None
                   else moment - candidate.stat().st_mtime)
        except (OSError, UnicodeDecodeError):
            continue
        if age <= max_age:
            continue
        try:
            candidate.unlink()
            removed += 1
        except OSError:
            pass
    return removed
