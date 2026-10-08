"""Git operations for work repository."""

import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

# The host classification rule, not a second copy of it: this is the function
# that decides which provider a hostname belongs to for token lookup, and a forge
# that reads as GitHub for credentials but GitLab for URLs would be a bug with no
# symptom until someone clicked. ``lmer_cli.tokens`` costs nothing to import
# (stdlib only), and ``work_repo.memory`` already reaches into ``lmer_cli`` the
# same way.
from lmer_cli.tokens import _is_github_host

# The credential scrub, imported for the same reason and from the same origin
# every other sink uses (lmer_platform.workrepo, clone_cache, gates): this
# module prints combined git output verbatim on the failure and retry paths,
# and the work-repo origin is a tokenized clone URL. One regex, one place.
from lmer_cli.container.clone_and_exec import _scrub_credentials

# The gate-in-flight marker (issue #201). Same reasoning as the token import
# above: the decision "is a long gate running right now?" has exactly one
# definition, and a second copy here would drift from the gates that write it.
from lmer_cli import gate_lock

from . import project_index, utils
from .run_state import (
    LEGACY_STATE_FILE,
    STATE_FILE,
    append_event,
    is_default_label,
    run_dir,
    run_rel_path_candidates,
    seeded_record_rel_paths,
)
from .specs_index import specs_rel_path
from .utils import no_run_context_error, task_target_dir, work_repo_layout
from .transactions import (
    checkout_lock, checkout_metadata_dir, inherited_transaction, pending_paths,
    published_paths, queue_paths,
)

PUSH_RETRIES = 3

#: The environment every git call this module makes runs with, over the
#: process's own. ``LC_ALL=C`` pins git's message locale: claim_push_once
#: classifies CAS outcomes by substring-matching English output, and a
#: localized "non-fast-forward" would misread as CLAIM_PUSH_ERROR (fail-closed
#: direction, but it would break the bounded re-evaluate loop). The two editor
#: variables pin ``rebase --continue`` to finish without an editor when the
#: index-conflict handler resumes a paused rebase: an exported ``GIT_EDITOR``
#: outranks ``-c core.editor``, so a host or container that exports one would
#: otherwise leave the rebase paused. The ``-c`` flags on that call stay as
#: belt and braces.
_GIT_BASE_ENV = {"LC_ALL": "C", "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}

#: Bound on OUR unpushed commits replaying onto a changed project README in
#: one pull — normally one — not on upstream's commits or the push attempts.
INDEX_CONFLICT_ROUNDS = 10

#: The index lines of the CLI surface (docs/WORK-REPO-SAFETY.md, the design spec).
INDEX_NUDGE = (
    "💡 this run has no name yet — work name <kebab-case> (or launch with lmer --name); "
    "the index lists it by its default label (or its goal or first log line) until then"
)
INDEX_SKIPPED = "⚠️  project index skipped: "
INDEX_LEFTOVER_SKIPPED = "⚠️  project index leftover skipped: "
INDEX_RESOLVED = "↻ regenerated project index on the merged head"

#: Where a deferred commit parks its trace until a commit can actually land
#: (issue #201). It lives beside the gate markers in /tmp, NOT in the run dir,
#: because writing the trace into the work repo is the very thing the deferral
#: exists to avoid: appending to a currently-clean tracked file adds a porcelain
#: entry, and the running suite's leak guard would report it as an appearance —
#: the same failure in a new costume. Flushed into ``events.jsonl`` by the next
#: commit that is allowed to proceed, so the trace lands *with* the commit that
#: resolves it and is pushed where the reviewer can see it.
DEFERRAL_QUEUE_NAME = "deferred-commits.jsonl"

# Outcomes of :func:`claim_push_once` — the claim-by-push compare-and-swap
# leg (docs/RUN-STATE.md §7). String constants rather than an enum so the
# values read literally in logs and test output.
CLAIM_PUSH_WON = "won"
CLAIM_PUSH_LOST_RACE = "lost-race"
CLAIM_PUSH_ERROR = "error"

# Markers git prints on a push the server refused because the remote ref
# advanced (non-fast-forward). Anything else on a failed push — transport,
# auth, missing remote — is a genuine error, never a lost race.
_NON_FAST_FORWARD_MARKERS = ("[rejected]", "non-fast-forward", "fetch first")

# Cap on how many stray entries ``report_uncommitted_work_items`` lists before
# collapsing the rest into a "... and N more" line, so a large dirty tree can
# never flood ``work commit``'s output.
UNTRACKED_REPORT_CAP = 10

#: The forges whose web path layout is known. Anything else is unknown, and
#: :func:`forge_web_url` answers None for it — see that function for why a guess
#: is worse than no link.
FORGE_GITLAB = "gitlab"
FORGE_GITHUB = "github"

#: The ONE definition of each forge's blob/tree path shape. GitLab namespaces
#: repository browsing under ``/-/`` (so a branch called ``blob`` cannot collide
#: with the route), GitHub does not. A second copy of this map is how one forge
#: keeps a bug the other one fixed, which is why the platform imports this module
#: instead of building URLs of its own.
_FORGE_PATH_PREFIX = {
    FORGE_GITLAB: "/-/",
    FORGE_GITHUB: "/",
}

#: What a checkout's branch reads as when git will not say — a detached HEAD, a
#: directory that is not a repo. Wrong on a work repo whose default branch is
#: named something else, and deliberately: a link to the wrong branch is fixable
#: by the human looking at it, while no link at all loses the artifact.
DEFAULT_WEB_BRANCH = "main"


def run_git_command(
    cmd: list[str], cwd: Path, check: bool = True, extra_env: Optional[dict] = None
) -> tuple[int, str]:
    """
    Run a git command and return exit code and output.

    Args:
        cmd: Git command and arguments
        cwd: Working directory for the command
        check: If True, raise exception on non-zero exit
        extra_env: Environment overlay — ``GIT_INDEX_FILE`` for the
            publication index, which is how a tree is assembled without
            touching the checkout's own index.

    Returns:
        Tuple of (exit_code, output)
    """
    try:
        # The base env (locale, editors) is the one place that reaches every
        # call: the push-safety tests' shim drops ``extra_env`` on purpose.
        result = subprocess.run(
            ["git"] + cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=check,
            env={**os.environ, **_GIT_BASE_ENV, **(extra_env or {})},
        )
        if result.returncode == 0:
            return result.returncode, result.stdout
        # Git reports errors on stderr; include it so a failure message is
        # never an empty string after the colon.
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.CalledProcessError as e:
        return e.returncode, ((e.stdout or "") + (e.stderr or "")).strip()


def _web_base_from_remote(remote: str) -> Optional[str]:
    """Web base URL (``https://host/project``) for a git remote URL.

    Any userinfo in the remote — the work repo's remote is typically
    tokenized, e.g. ``https://oauth2:TOKEN@git.example.com/agents/work.git``
    — is STRIPPED: only the hostname (plus port) and project path survive,
    so a derived URL can never leak the token. ssh remotes, both the
    ``ssh://git@host/path.git`` scheme form and the scp-style
    ``git@host:path.git``, normalize to https. Returns None when no
    host/project can be derived (e.g. a local-path remote).
    """
    if "://" in remote:
        parts = urlsplit(remote)
        host = parts.hostname
        if not host:
            return None
        if parts.port:
            host = f"{host}:{parts.port}"
        project = parts.path.strip("/")
    else:
        match = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", remote)
        if match is None:
            return None
        host, project = match.group(1), match.group(2).strip("/")
    if project.endswith(".git"):
        project = project[: -len(".git")]
    if not project:
        return None
    return f"https://{host}/{project}"


def detect_forge(host: Optional[str]) -> Optional[str]:
    """Which forge a hostname belongs to (:data:`FORGE_GITLAB` /
    :data:`FORGE_GITHUB`), or None when it cannot be told.

    GitHub is classified by :func:`lmer_cli.tokens._is_github_host` — the same
    rule token lookup uses, so a host cannot be GitHub for credentials and
    something else for URLs. GitLab is ``gitlab.com``, its subdomains, and the
    self-hosted convention of naming the instance ``gitlab.<domain>`` or
    ``gitlab-<something>.<domain>``.

    Everything else — ``git.example.com``, a GitHub Enterprise Server on a
    custom name — is None, because the only honest thing to say about an
    unrecognised host is that its path layout is unknown. A port is accepted
    and ignored, so a base URL's authority can be passed straight in.
    """
    if not host:
        return None
    name = host.strip().lower().split(":", 1)[0]
    if not name:
        return None
    if _is_github_host(name):
        return FORGE_GITHUB
    if name == "gitlab.com" or name.endswith(".gitlab.com"):
        return FORGE_GITLAB
    first = name.split(".", 1)[0]
    if first == "gitlab" or first.startswith("gitlab-"):
        return FORGE_GITLAB
    return None


def forge_web_url(
    base: str,
    ref: str,
    rel_path: str = "",
    *,
    is_dir: bool = False,
    forge: Optional[str] = None,
    default_forge: Optional[str] = None,
) -> Optional[str]:
    """A forge's browse URL for *rel_path* at *ref*, or None for an unknown forge.

    *base* is ``https://<host>/<project>`` — the credential-free form
    :func:`_web_base_from_remote` produces, and the only form that may be passed
    here, since whatever this returns is rendered as a clickable link. The forge
    is derived from *base*'s host (:func:`detect_forge`); *rel_path* empty means
    the repository root, which is always a tree.

    *forge* names the forge outright and skips detection, because the operator
    setting it exists for the cases detection gets wrong in *either* direction:
    a GitHub Enterprise Server sits on any hostname, and a self-hosted GitLab
    could be named after either. A value that is no known forge — the platform's
    ``work_repo_forge=none`` — resolves to no prefix and therefore no URL, which
    is how that setting switches links off.

    *default_forge* is what an unrecognised host falls back to when *forge* is
    unset, and both callers that build these links pass GitLab: a work repo on a
    hostname that says nothing about it (``git.example.com``) is a self-hosted
    GitLab in every deployment either has met, and the two surfaces disagreeing
    about the same run dir — linked in ``work``, plain names in the platform —
    was worse than either answer alone. Passing nothing still yields None, which
    is what a caller with no configured opt-out to offer should do.
    """
    forge = forge or detect_forge(urlsplit(base).hostname) or default_forge
    prefix = _FORGE_PATH_PREFIX.get(forge)
    if not base or prefix is None:
        return None
    ref_q = quote(ref or DEFAULT_WEB_BRANCH, safe="/")
    if not rel_path:
        return f"{base}{prefix}tree/{ref_q}"
    kind = "tree" if is_dir else "blob"
    return f"{base}{prefix}{kind}/{ref_q}/{quote(rel_path, safe='/')}"


def checkout_branch(repo_path) -> str:
    """The branch *repo_path* is on, or :data:`DEFAULT_WEB_BRANCH`.

    ``symbolic-ref`` (not ``rev-parse``) so an unborn initial branch still names
    itself; a detached HEAD, a directory that is not a repo and a missing
    directory all fall back. Never raises — this only ever decides which ref a
    link points at.
    """
    try:
        rc, branch = run_git_command(
            ["symbolic-ref", "--short", "HEAD"], Path(repo_path), check=False
        )
    except Exception:
        return DEFAULT_WEB_BRANCH
    return branch.strip() if rc == 0 and branch.strip() else DEFAULT_WEB_BRANCH


def web_url_for(path) -> Optional[str]:
    """Forge web URL for a path inside the work-repo checkout, or None.

    Maps an absolute path under ``LMER_WORK_REPO_PATH`` (default ``/work``)
    to ``https://<host>/<project>/-/blob/<branch>/<relpath>`` for files and
    ``/-/tree/<branch>/<relpath>`` for directories — the clickable form a
    human on a phone can actually open, instead of a container path. The
    web base comes from the checkout's ``origin`` remote with credentials
    stripped (:func:`_web_base_from_remote`); the branch is the current
    branch, falling back to ``main`` when detection fails (detached HEAD).

    The path shape comes from :func:`forge_web_url`, so a work repo on GitHub
    gets GitHub's ``/blob/`` rather than GitLab's ``/-/blob/``. A host that
    classifies as neither keeps GitLab's shape, which is what these links have
    always produced — a self-hosted GitLab is commonly at ``git.<domain>``, and
    dropping its links would be a regression for the deployments that have them.

    Fail-soft by contract: any problem — no work repo, no origin remote, a
    remote with no host, a path outside or missing from the checkout —
    returns None and callers print the plain path as before. Never raises.
    """
    try:
        work_repo_path = Path(
            os.environ.get("LMER_WORK_REPO_PATH", "/work")
        ).resolve()
        target = Path(path).resolve()
        if not target.exists():
            return None
        if target != work_repo_path and work_repo_path not in target.parents:
            return None
        rc, remote = run_git_command(
            ["remote", "get-url", "origin"], work_repo_path, check=False
        )
        if rc != 0 or not remote.strip():
            return None
        base = _web_base_from_remote(remote.strip())
        if base is None:
            return None
        rel = "" if target == work_repo_path else target.relative_to(
            work_repo_path
        ).as_posix()
        return forge_web_url(
            base,
            checkout_branch(work_repo_path),
            rel,
            is_dir=target.is_dir(),
            default_forge=FORGE_GITLAB,
        )
    except Exception:
        return None


def run_dir_push_status(run_dir) -> tuple[bool, bool]:
    """``(dirty, unpushed)`` for a run dir inside the work-repo checkout.

    The minimal push-compliance predicate of the Stop-hook guard's trigger 2
    (``hooks/run_state_guard.py`` — hooks import no project code, so the
    check is mirrored here rather than imported from there): dirty when the
    dir-scoped ``git status --porcelain -- .`` shows anything; unpushed when
    commits touching the dir exist that the upstream lacks. Both commands
    run from inside the run dir with a ``-- .`` pathspec, so a busy work
    repo cannot trip the check on other projects' changes.

    Fail-soft by contract: a missing dir, a non-git dir, no upstream, or any
    other git problem reads as ``(False, False)`` — callers advise only on a
    *certain* noncompliance, never on uncertainty. Never raises.
    """
    try:
        run_dir = Path(run_dir)
        if not run_dir.is_dir():
            return (False, False)
        rc, status = run_git_command(
            ["status", "--porcelain", "--", "."], run_dir, check=False
        )
        dirty = rc == 0 and bool(status.strip())
        rc, ahead = run_git_command(
            ["rev-list", "--count", "@{upstream}..HEAD", "--", "."],
            run_dir,
            check=False,
        )
        unpushed = False
        if rc == 0:
            try:
                unpushed = int(ahead.strip()) > 0
            except ValueError:
                unpushed = False
        return (dirty, unpushed)
    except Exception:
        return (False, False)


def _has_tracked_files(work_repo_path: Path, rel_path: str) -> bool:
    """True when git tracks anything under rel_path — even if it is gone
    from disk (pending deletions still need staging)."""
    rc, output = run_git_command(
        ["ls-files", "--", rel_path], work_repo_path, check=False
    )
    return rc == 0 and bool(output.strip())


def stageable_paths(work_repo_path: Path, paths: list[str]) -> list[str]:
    """The subset of paths `git add` can be pointed at without erroring.

    A path that neither exists on disk nor holds tracked files makes
    ``git add -A -- <path>`` exit 128 ("pathspec did not match any files"),
    which would fail every caller outright — e.g. the stale bare-slug
    candidate from run_rel_path_candidates() after a run-dir rename. A
    tracked path missing from disk is KEPT so pending deletions still get
    staged (a rename lands as a move, not a duplicate).
    """
    return [
        p
        for p in paths
        if (work_repo_path / p).exists() or _has_tracked_files(work_repo_path, p)
    ]


def _deferral_queue_path() -> Path:
    """The parked-deferrals file, beside the gate markers it belongs to."""
    return gate_lock.lock_dir() / DEFERRAL_QUEUE_NAME


def _current_run_dir() -> Optional[Path]:
    """The run dir, or None — a broken/absent run must never break a commit."""
    try:
        return run_dir()
    except Exception:
        return None


def _queue_deferral(rdir: Optional[Path], marker: dict, paths: list[str]) -> None:
    """Park one deferral record for the next commit to flush. Fail-soft.

    A deferral that left no trace would be a lie of omission: the caller was
    told "0" and its files are still only on disk. The record carries which
    gate caused it and what was not committed, so the flushed event answers
    both questions the reviewer will have.
    """
    if rdir is None:
        return
    try:
        record = {
            "run_dir": str(rdir),
            "gate": marker.get("gate"),
            "pid": marker.get("pid"),
            "started_at": marker.get("started_at"),
            "paths": list(paths),
        }
        path = _deferral_queue_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError):
        pass


def _flush_deferrals(rdir: Optional[Path]) -> int:
    """Append parked deferrals for *rdir* as `commit_deferred` events.

    Called on the way into a commit that is *allowed* to proceed, so the events
    are staged by that same commit and reach the work repo with it. Records for
    other run dirs are kept for their own runs. Fail-soft; returns how many
    events were written so the caller can say so.
    """
    if rdir is None:
        return 0
    path = _deferral_queue_path()
    try:
        if not path.exists():
            return 0
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0

    mine: list[dict] = []
    others: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue  # torn line — drop it rather than replay it forever
        if isinstance(record, dict) and record.get("run_dir") == str(rdir):
            mine.append(record)
        else:
            others.append(line)

    if not mine:
        return 0

    written = 0
    for record in mine:
        try:
            gate = record.get("gate") or "a gate"
            append_event(
                rdir,
                "commit_deferred",
                note=f"work-repo commit deferred while {gate} was in flight",
                data={
                    "gate": gate,
                    "pid": record.get("pid"),
                    "paths": record.get("paths") or [],
                },
            )
            written += 1
        except (OSError, ValueError, TypeError):
            break  # leave the rest parked rather than losing them

    try:
        if written >= len(mine) and not others:
            path.unlink()
        else:
            remaining = others + [
                json.dumps(r, ensure_ascii=False) for r in mine[written:]
            ]
            path.write_text(
                "\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8"
            )
    except (OSError, ValueError, TypeError):
        pass
    return written


def _report_deferral(marker: dict, paths: list[str]) -> None:
    """Tell the session, in the moment, why nothing was committed."""
    print(f"⏸️  Work-repo commit deferred: {gate_lock.describe_marker(marker)}")
    print(
        "   Committing now would move tracked files under the running gate and "
        "fail its /work isolation guard (issue #201)."
    )
    if paths:
        print(f"   Left on disk, uncommitted: {', '.join(paths)}")
    else:
        print("   Existing local commits are awaiting publication.")
    print(
        "   Run `work commit` once the gate finishes — the stop hook will not "
        "let the session end until it lands."
    )
    print(
        "   Other work-repo writes (`work log`, `work event`, state updates) "
        "are safe meanwhile: they journal themselves and the suite's guard "
        "attributes them to this session, not to the tests (issue #233)."
    )


def _git_failure(operation: str, label: str, rc: int, output: str) -> int:
    print(f"❌ {operation} failed ({label}): {_scrub_credentials(output)}", file=sys.stderr)
    return rc or 1


def _git_directory(repo_path: Path, label: str) -> Optional[Path]:
    rc, output = run_git_command(["rev-parse", "--absolute-git-dir"], repo_path, check=False)
    if rc != 0 or not output.strip():
        _git_failure("git rev-parse --absolute-git-dir", label, rc, output)
        return None
    return Path(output.strip())


def check_commit_ready(repo_path: Path, label: str = "work repo") -> int:
    """Refuse interrupted operations before copying files or staging resolutions."""
    git_dir = _git_directory(repo_path, label)
    if git_dir is None:
        return 1
    for state in (
        "rebase-merge", "rebase-apply", "MERGE_HEAD",
        "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer",
    ):
        if (git_dir / state).exists():
            return _git_failure(
                "repository state check", label, 1,
                f"{state} is in progress at {repo_path}; "
                "resolve or abort it manually before retrying.",
            )
    rc, output = run_git_command(["symbolic-ref", "--quiet", "HEAD"], repo_path, check=False)
    if rc != 0:
        return _git_failure(
            "repository state check", label, rc,
            f"detached HEAD or unreadable branch at {repo_path}; "
            f"check out the intended branch.\n{output}",
        )
    rc, output = run_git_command(["ls-files", "--unmerged"], repo_path, check=False)
    if rc != 0 or output.strip():
        return _git_failure("unmerged index check", label, rc, output)
    return 0


def _check_staged_conflicts(repo_path: Path, label: str) -> int:
    # Check the whole index: git commit also includes previously staged paths.
    rc, output = run_git_command(
        [
            "-c", "core.whitespace=-blank-at-eol,-blank-at-eof,-space-before-tab",
            "diff", "--cached", "--check", "--text", "--no-ext-diff", "--no-textconv",
        ],
        repo_path, check=False,
    )
    if rc != 0:
        return _git_failure(
            "staged conflict marker check", label, rc,
            output + "\nResolve leftover markers. For legitimate prose, indent or "
            "lengthen seven-character marker lines (<, =, >, or |).",
        )
    return 0


def _index_snapshot(repo_path: Path) -> tuple[Path, Optional[bytes]]:
    rc, output = run_git_command(["rev-parse", "--git-path", "index"], repo_path, check=False)
    if rc != 0 or not output.strip():
        raise OSError(output or "Cannot locate the Git index")
    index = repo_path / output.strip()
    return index, index.read_bytes() if index.exists() else None


def _restore_index(index: Path, saved: Optional[bytes], label: str) -> None:
    lock = index.with_name(index.name + ".lock")
    try:
        with lock.open("xb") as stream:
            try:
                if saved is None:
                    index.unlink(missing_ok=True)
                else:
                    stream.write(saved)
                    stream.flush()
                    os.replace(lock, index)
            finally:
                lock.unlink(missing_ok=True)
    except OSError as exc:
        _git_failure("index restoration", label, 1, str(exc))


def _stage_and_check(repo_path: Path, paths: list[str], label: str) -> tuple[int, str]:
    """Restore the exact previous staging state if preparation is refused."""
    try:
        index, saved = _index_snapshot(repo_path)
    except OSError as exc:
        return _git_failure("index snapshot", label, 1, str(exc)), ""
    rc, output = run_git_command(["add", "-A", "--", *paths], repo_path, check=False)
    if rc != 0:
        return _git_failure("git add", label, rc, output), output
    rc, output = run_git_command(
        ["status", "--porcelain", "--", *paths], repo_path, check=False
    )
    if rc != 0:
        _git_failure("git status", label, rc, output)
    else:
        rc = _check_staged_conflicts(repo_path, label)
    if rc != 0:
        _restore_index(index, saved, label)
    return rc, output


def _push_pending_commits(repo_path: Path, label: str) -> int:
    rc, output = run_git_command(
        ["rev-list", "--max-count=1", "@{upstream}..HEAD"], repo_path, check=False
    )
    if rc != 0:
        return _git_failure("pending commit check", label, rc, output)
    if not output.strip():
        return _verify_published(repo_path, label, "HEAD")
    print(f"📤 Publishing existing local commits ({label})...")
    rc = _push_with_rebase_retries(repo_path, label)
    if rc == 0:
        print(f"✅ Published pending commits ({label})")
    return rc


def _publication_destination(repo_path: Path, label: str) -> Optional[tuple[str, str]]:
    rc, branch = run_git_command(["symbolic-ref", "--quiet", "HEAD"], repo_path, check=False)
    if rc != 0:
        _git_failure("publication branch check", label, rc, branch)
        return None
    rc, output = run_git_command(
        [
            "for-each-ref", "--format=%(upstream:remotename)%00%(upstream:remoteref)",
            branch.strip(),
        ],
        repo_path, check=False,
    )
    if rc != 0:
        _git_failure("upstream destination check", label, rc, output)
        return None
    remote, _, ref = output.strip().partition("\0")
    if not remote or remote == "." or not ref.startswith("refs/heads/"):
        _git_failure(
            "upstream destination check", label, 1,
            "the current branch has no remote branch upstream configured",
        )
        return None
    return remote, ref


def _verify_published(
    repo_path: Path, label: str, commit: str,
    destination: Optional[tuple[str, str]] = None,
) -> int:
    destination = destination or _publication_destination(repo_path, label)
    if destination is None:
        return 1
    remote, ref = destination
    # Fetch the actual remote branch, including when a tracking ref is stale or deleted.
    rc, output = run_git_command(["fetch", "--no-tags", remote, ref], repo_path, check=False)
    if rc != 0:
        return _git_failure("post-push git fetch", label, rc, output)
    rc, output = run_git_command(
        ["merge-base", "--is-ancestor", commit, "FETCH_HEAD"], repo_path, check=False,
    )
    if rc != 0:
        return _git_failure(
            "remote reachability check", label, rc,
            f"commit {commit} was not confirmed on the fetched upstream.\n{output}",
        )
    return 0


# ---------------------------------------------------------------------------
# The project README index at the choke point
# ---------------------------------------------------------------------------
#
# Every durability push and every pending replay crosses
# _commit_work_path_locked, so that is where a project's README index block is
# rendered from the records the commit will contain and staged with them. The
# kernel (work_repo.project_index) renders and splices; nothing here composes a
# line. Everything here is fail-soft to a fault: a README the generator must
# not touch is skipped with one warning, and no exception raised while
# regenerating may change a push's exit code — the record publishes exactly
# as it did before the index existed (design spec, "Concurrency and failure
# handling").


def _index_project_rels(work_repo_path: Path) -> list[str]:
    """Repo-relative dirs of every project whose README carries an index block.

    Registered projects first, in registry order, then the host buckets —
    :func:`work_repo.utils.work_repo_project_rels`, the one module in the
    work CLI that spells the core's walk (issue #353 D20; the core is
    imported there, lazily, so the import direction stays ``work_repo ->
    lmer_schema``). ``[]`` on a legacy tree (no manifest) and when the schema
    core is not installed. When the manifest is present but unusable, this
    session's own base — the host bucket the layout resolves to — so its push
    still indexes where it writes.
    """
    try:
        return utils.work_repo_project_rels(Path(work_repo_path))
    except ImportError:
        return []
    except Exception:
        try:
            base = _rel_to_root(work_repo_layout().base, Path(work_repo_path))
        except Exception:
            return []
        return [base] if base else []


def _porcelain_paths(output: str) -> list[str]:
    """The paths a ``status --porcelain -z`` listing names, a rename's source included."""
    paths: list[str] = []
    fields = output.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if ("R" in status or "C" in status) and index < len(fields):
            paths.append(fields[index])
            index += 1
    return paths


def _dirty_index_rels(work_repo_path: Path) -> list[str]:
    """Project READMEs a tool left dirty: generated (both markers), no marker line.

    The crash shape: a session wrote its record and its README, then the
    container went away before the commit. The next push from any session in
    the checkout commits that README rather than letting ``pull --rebase
    --autostash`` carry it. A human's markerless draft is never swept (the
    project's next record push appends the block to it); a README whose hand
    text holds a conflict-marker line, or whose block a newer runtime wrote
    (this one must not downgrade it), is named and left alone. Only what the
    refresh can actually stage is selected. No git call when the tree has no
    project dirs: a bare ``git status --porcelain --`` would report the whole
    worktree. The untracked mode is named on the command line: a leftover
    README is untracked until its first commit, and a checkout whose config
    says ``status.showUntrackedFiles=no`` would otherwise hide it. A README
    whose project directory is outside the write boundary (a tracked
    project dir later replaced by a symlink is listed as deleted, and the
    file read through the link may carry a block) is named and left alone.
    """
    try:
        projects = _index_project_rels(work_repo_path)
        if not projects:
            return []
        readmes = [f"{rel}/{project_index.README_NAME}" for rel in projects]
        rc, output = run_git_command(
            ["status", "--porcelain", "-z", "--untracked-files=normal", "--", *readmes],
            work_repo_path, check=False,
        )
        if rc != 0:
            return []
        leftovers: list[str] = []
        for rel in _porcelain_paths(output):
            if rel not in readmes or rel in leftovers:
                continue
            boundary = project_index.project_dir_refusal(
                work_repo_path, rel[: -(len(project_index.README_NAME) + 1)]
            )
            if boundary is not None:
                print(f"{INDEX_LEFTOVER_SKIPPED}{rel} — {boundary}")
                continue
            path = Path(work_repo_path) / rel
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                block = project_index.extract_block(text)
            except project_index.MalformedMarkers:
                continue
            if block is None:
                continue
            token = project_index.block_format(block)
            if token is not None and token > project_index.FORMAT:
                print(
                    f"{INDEX_LEFTOVER_SKIPPED}{rel} — index block is of a newer format "
                    f"({token} > {project_index.FORMAT}); upgrade lmer"
                )
                continue
            if project_index.hand_text_has_marker(text):
                print(
                    f"{INDEX_LEFTOVER_SKIPPED}{rel} — hand text holds a conflict-marker line "
                    "(seven = < > or |); indent or lengthen the line"
                )
                continue
            leftovers.append(rel)
        return leftovers
    except Exception as exc:
        print(f"{INDEX_LEFTOVER_SKIPPED}{exc}")
        return []


def _project_heading(project_rel: str) -> str:
    """What a README created from nothing is headed with: the short name — a
    registered project's rel IS its name, and a bucket's last segment is the
    repository's."""
    return project_rel.rsplit("/", 1)[-1]


def _ignored(work_repo_path: Path, rel: str) -> bool:
    """Whether the checkout's ignore rules would make ``git add`` refuse *rel*."""
    rc, _ = run_git_command(["check-ignore", "-q", "--", rel], work_repo_path, check=False)
    return rc == 0


def _refresh_project_indexes(
    work_repo_path: Path, paths: list[str], leftovers: list[str]
) -> tuple[list[str], list[tuple[str, Optional[bytes]]]]:
    """Render the README index block of every project this commit touches.

    The projects are those owning a staged ``_runs`` path plus the projects
    of the swept *leftovers*. Each is rendered over the records whose state
    file is tracked or lies under a staged path — exactly what the commit
    will contain, so no row can point at a directory origin will not have —
    and written only when the bytes differ. Returns ``(README rels to stage,
    [(rel, previous bytes or None)] for every file rewritten)`` so a refused
    staging can put the files back. A README that is ignored, a symlink, a
    directory, malformed, of a newer format, or whose hand text holds a
    conflict-marker line is skipped with one warning and the record still
    publishes. A project directory outside the write boundary
    (:func:`work_repo.project_index.project_dir_refusal`: a symlink, a path
    resolving outside the checkout, a ``_runs`` record) is skipped the same
    way, before any git call or write in it. Any other exception restores
    what was already rewritten and yields ``([], [])`` after a warning: the
    push's exit code is never this function's to change.
    """
    written: list[tuple[str, Optional[bytes]]] = []
    current: Optional[str] = None
    try:
        projects = _index_project_rels(work_repo_path)
        if not projects:
            return [], []
        targets = project_index.project_rels_for_paths(paths, projects)
        for rel in leftovers:
            project = rel.rsplit("/", 1)[0]
            if project not in targets:
                targets.append(project)
        staged: list[str] = []
        for project in targets:
            current = readme_rel = f"{project}/{project_index.README_NAME}"
            try:
                boundary = project_index.project_dir_refusal(work_repo_path, project)
                if boundary is not None:
                    raise project_index.IndexSkipped(boundary)
                if _ignored(work_repo_path, readme_rel):
                    raise project_index.IndexSkipped("ignored by the checkout (git add would refuse it)")
                include = project_index.tracked_record_filter(work_repo_path, project, staged=paths)
                changed, previous = project_index.write_project_readme(
                    Path(work_repo_path) / project, include=include, project_rel=project,
                    heading=_project_heading(project),
                )
            except project_index.IndexSkipped as exc:
                print(f"{INDEX_SKIPPED}{readme_rel} — {exc}")
                continue
            if changed:
                written.append((readme_rel, previous))
            if changed or readme_rel in leftovers:
                staged.append(readme_rel)
        return staged, written
    except Exception as exc:
        print(f"{INDEX_SKIPPED}{current or 'all projects'} — {exc}")
        _restore_index_files(work_repo_path, written)
        return [], []


def _restore_index_files(work_repo_path: Path, previous: list[tuple[str, Optional[bytes]]]) -> None:
    """Put every README the regeneration rewrote back to its pre-call bytes,
    removing a file that did not exist before. Fail-soft."""
    for rel, data in previous:
        path = Path(work_repo_path) / rel
        try:
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
        except OSError as exc:
            print(f"⚠️  project index restore failed: {rel} — {exc}", file=sys.stderr)


def _nudge_unnamed(rdir: Optional[Path]) -> None:
    """One stdout line while this run's record is readable and has no name.

    Every durability push prints it (R4(b) of the design brief): the index
    lists an unnamed run by its goal or first log line, and `work name` is
    the cure. Read with the kernel's lock-free fast reader; fail-soft.
    """
    if rdir is None:
        return
    try:
        rdir = Path(rdir)
        path = next((rdir / name for name in (STATE_FILE, LEGACY_STATE_FILE)
                     if (rdir / name).exists()), None)
        if path is None:
            return
        state = project_index.read_state_fast(path)
        if not isinstance(state, dict):
            return
        name = state.get("name")
        if (
            name is None
            or (isinstance(name, str) and not name.strip())
            or is_default_label(state)
        ):
            print(INDEX_NUDGE)
    except Exception:
        pass


def commit_work_path(target_path, commit_message=None, allow_during_gate=False) -> int:
    """Serialize publication and replay complete deferred transition paths.

    Includes deferred transitions from other sessions in this checkout: their
    shared retirement marker can only be published with all its archive/live
    changes. Ordinary pre-stage refusals do not enroll unrelated paths for
    later retries; explicit transitions and actual deferrals retain their
    requests until verified publication.
    """
    root = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
    if not root.exists():
        print(f"❌ Work repository not found at {root}", file=sys.stderr)
        return 1
    paths = [target_path] if isinstance(target_path, str) else list(target_path)
    try:
        if inherited_transaction(root):
            queue_paths(root, paths)
            _queue_deferral(_current_run_dir(), {"gate": "invoking Git transaction"}, paths)
            print("⏸️  Work-repo commit deferred until the invoking Git transaction finishes")
            return 0
        with checkout_lock(root):
            entries, deferred_paths = pending_paths(root)
            paths.extend(path for path in deferred_paths if path not in paths)
            return _commit_work_path_locked(paths, commit_message, allow_during_gate, entries)
    except (OSError, ValueError) as exc:
        print(f"❌ Work-repo commit transaction failed: {exc}", file=sys.stderr)
        return 1


def _commit_work_path_locked(
    target_path,
    commit_message: Optional[str] = None,
    allow_during_gate: bool = False,
    pending_entries=(),
) -> int:
    """
    Sync one or more paths in the work repo: add -> commit -> rebase -> push.

    Stages additions, modifications, and deletions under the given paths
    relative to the work repo root. Git commit also includes previously
    staged paths; unrelated unstaged edits remain local.
    If staging produces no changes, retries any unpushed commits —
    the no-change check is scoped to the given paths too, so an unrelated
    dirty file (e.g. a per-session ``log.yaml``) does not trigger a spurious
    empty commit.

    Commit the selected changes before rebasing so conflicts retain a local
    commit. Rebase autostashes unrelated tracked edits; push races trigger
    another rebase before retrying.

    Args:
        target_path: Path (or list of paths) within the work repo to stage,
            relative to its root (e.g. ``github.com/owner/repo/review/pr-123``).
            Paths that neither exist on disk nor hold tracked files are
            skipped; a tracked path missing from disk is KEPT so that
            deletions — e.g. the old name after a run-dir rename — get
            staged and the move lands as a move, not a duplicate.
        commit_message: Optional commit message (defaults to auto-generated
            from the first path).
        allow_during_gate: Commit even while a gate holds the in-flight marker
            (issue #201). Two callers set it, both deliberately: the release
            CAS claim path (arbitration, not durability — deferring it would
            wedge a release) and ``work session-end`` (the session and its
            container are going away, so the gate whose window we would be
            protecting is being torn down with them; durability wins).

    Returns:
        Exit code (0 for success, non-zero for failure). A deferral is 0 — it
        is not a failure — but it leaves a ``commit_deferred`` trace behind
        rather than passing silently as success.
    """
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))

    if not work_repo_path.exists():
        print(f"❌ Work repository not found at {work_repo_path}", file=sys.stderr)
        return 1

    rc = check_commit_ready(work_repo_path)
    if rc != 0:
        return rc

    all_paths = [target_path] if isinstance(target_path, str) else list(target_path)
    paths = stageable_paths(work_repo_path, all_paths)
    # 0. Gate-in-flight check (issue #201), BEFORE any git write. A gate leaves
    # the run dir dirty via its own receipt, which arms the stop hook, whose
    # mandated commit then sweeps tracked files out from under the running
    # suite and fails its /work isolation guard. Deferring here covers every
    # durability push — `work commit` and the implicit ones behind `work state
    # set` / `goal` / `artifact` / `ledger set` — not just the one path that
    # happened to bite first.
    marker = None if allow_during_gate else gate_lock.active_gate()
    if marker is not None:
        # A clean index may still hide commits left by a failed push.
        pending = ""
        if paths:
            rc, pending = run_git_command(
                ["status", "--porcelain", "--", *paths], work_repo_path, check=False
            )
            if rc != 0:
                return _git_failure("git status", "work repo", rc, pending)
        if not pending.strip():
            rc, pending = run_git_command(
                ["rev-list", "--max-count=1", "@{upstream}..HEAD"],
                work_repo_path, check=False,
            )
            if rc != 0:
                return _git_failure("pending commit check", "work repo", rc, pending)
            if not pending.strip():
                return 0
        queue_paths(work_repo_path, all_paths)
        _report_deferral(marker, paths)
        _queue_deferral(_current_run_dir(), marker, paths)
        return 0

    # Every durability push that gets past the gate — the no-op and
    # leftover-only branches included — reminds an unnamed run of its name,
    # and sweeps a generated README a crashed session left dirty.
    rdir = _current_run_dir()
    _nudge_unnamed(rdir)
    leftovers = _dirty_index_rels(work_repo_path)
    if not paths and not leftovers:
        print("No existing paths to commit in work repository")
        return published_paths(pending_entries, _push_pending_commits(work_repo_path, "work repo"))

    # Parked deferrals land as events now, so this commit carries the trace of
    # the commits it is standing in for.
    flushed = _flush_deferrals(rdir)
    if flushed:
        print(
            f"↩️  Recording {flushed} commit deferred during a gate window "
            "(issue #201); this commit carries the trace."
        )

    # The project README index blocks, rendered from the records this commit
    # will contain and staged with them; never persisted into a pending entry
    # (recomputed per call), so pending-path semantics are untouched.
    index_rels, previous = _refresh_project_indexes(work_repo_path, paths, leftovers)
    paths = paths + [rel for rel in index_rels if rel not in paths]
    if not paths:
        # Only a leftover got this far and the refresh could not stage it (a
        # skip, or a failure that put it back): nothing is ours to commit, and
        # an empty pathspec would hand `git add -A` the whole shared worktree
        # — every sibling's record and anything else lying around.
        print("No existing paths to commit in work repository")
        return published_paths(pending_entries, _push_pending_commits(work_repo_path, "work repo"))

    print(f"➕ Staging {', '.join(paths)} in work repository...")
    rc, status_output = _stage_and_check(work_repo_path, paths, "work repo")
    if rc != 0:
        # The git index is already back (_stage_and_check); the README bytes
        # follow, so a refused push leaves the file exactly as it found it.
        _restore_index_files(work_repo_path, previous)
        return rc
    if not status_output.strip():
        print("No new file changes to commit in work repository")
        return published_paths(pending_entries, _push_pending_commits(work_repo_path, "work repo"))

    # 2. Commit
    if commit_message is None:
        commit_message = f"Update work repo: {paths[0]}"
    print(f"💾 Committing changes to work repository...")
    rc, output = run_git_command(["commit", "-m", commit_message], work_repo_path, check=False)
    if rc != 0:
        print(f"❌ git commit failed (work repo): {_scrub_credentials(output)}", file=sys.stderr)
        return rc

    # 3. Integrate the remote and push, rebasing between attempts — many
    # sessions and host-side tools push here concurrently.
    print(f"📤 Pushing changes to work repository...")
    rc = _push_with_rebase_retries(work_repo_path, "work repo")
    if rc == 0:
        print("✅ Successfully committed and pushed changes to work repository")
    return published_paths(pending_entries, rc)


# ---------------------------------------------------------------------------
# Publishing without touching the live checkout (§7)
# ---------------------------------------------------------------------------
#
# A claim used to be published the way an ordinary commit is: rebase the local
# branch onto the fetched head, write the claim into the worktree, commit,
# push. A dirty tracked file makes `pull --rebase` refuse outright, so the
# transaction stashed the checkout first — and a work-repo checkout is SHARED
# (spawn-harness children run beside their parent against one /work), so that
# stash captured every sibling session's uncommitted record, and a restore
# that could not put them back left them nowhere but the stash.
#
# What follows publishes a commit without reading or writing the live index,
# HEAD or worktree: the tree is assembled in a temporary index seeded from the
# fetched remote head, the commit is created as an object with `commit-tree`,
# and that object is pushed. So a claim cannot capture, move or lose anything
# of anyone else's, whatever happens to it, and a lost race has nothing to
# roll back. The arbitration is unchanged: one plain push, and the server's
# non-fast-forward rejection is the answer.


def _git_raw(
    repo_path: Path, args: list[str], *, stdin: Optional[bytes] = None,
    extra_env: Optional[dict] = None,
) -> tuple[int, bytes]:
    """``git`` with byte-exact input and output — content must round-trip."""
    result = subprocess.run(
        ["git", *args], cwd=str(repo_path), input=stdin, capture_output=True,
        env={**os.environ, **_GIT_BASE_ENV, **(extra_env or {})},
    )
    if result.returncode == 0:
        return 0, result.stdout
    return result.returncode, result.stdout + result.stderr


def fetch_publication_head(repo_path: Path, label: str) -> tuple[bool, str]:
    """Fetch the remote head a transaction arbitrates against, and name it.

    ``(True, sha)``, or ``(False, detail)``. An unreachable remote fails here,
    before anything is built or pushed, so a dead remote can never be mistaken
    for a lost race — a release fails closed, it does not proceed unlocked.
    """
    destination = _publication_destination(repo_path, label)
    if destination is None:
        return False, "the current branch has no remote branch upstream configured"
    remote, ref = destination
    rc, output = run_git_command(["fetch", "--no-tags", remote, ref], repo_path, check=False)
    if rc != 0:
        return False, f"git fetch failed: {_scrub_credentials(output)}"
    rc, output = run_git_command(
        ["rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"], repo_path, check=False
    )
    if rc != 0 or not output.strip():
        return False, f"cannot resolve the fetched head: {output}"
    return True, output.strip()


def published_file(repo_path: Path, commit: str, rel: str) -> tuple[bool, str]:
    """One file's text at *commit*: ``(True, text)``, or ``(False, "")`` when
    the path does not exist there. Reads the object store, never the worktree."""
    rc, _ = run_git_command(["cat-file", "-e", f"{commit}:{rel}"], repo_path, check=False)
    if rc != 0:
        return False, ""
    rc, output = run_git_command(["show", f"{commit}:{rel}"], repo_path, check=False)
    if rc != 0:
        raise OSError(f"cannot read {rel} at {commit}: {output}")
    return True, output


@contextmanager
def _publication_index(repo_path: Path):
    """A private index file, outside every commit path set, removed after use.

    It lives beside the checkout's own bookkeeping (never in the worktree, so
    no `work commit` can stage it), and `GIT_INDEX_FILE` points the plumbing
    at it — the checkout's index is neither read nor written.
    """
    directory = checkout_metadata_dir(repo_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"lmer-publish-index.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        yield {"GIT_INDEX_FILE": str(path)}
    finally:
        path.unlink(missing_ok=True)


def _replace_subtree(
    repo_path: Path, env: dict, rel: str, source: str
) -> Optional[str]:
    """Put *source*'s copy of the directory *rel* into the publication index.

    A tree-to-tree copy: file modes, symlinks and binary content cross over
    exactly as Git recorded them, with no blob rewritten and no worktree read.
    Returns a failure detail, or None on success.
    """
    rc, listed = run_git_command(["ls-files", "-z", "--", rel], repo_path, check=False, extra_env=env)
    if rc != 0:
        return f"cannot inspect {rel}: {listed}"
    existing = [entry for entry in listed.split("\0") if entry]
    if existing:
        # --force-remove, not `git rm`: the entry goes whatever the worktree
        # holds, because the worktree is not this transaction's business.
        rc, output = run_git_command(
            ["update-index", "--force-remove", "--", *existing],
            repo_path, check=False, extra_env=env,
        )
        if rc != 0:
            return f"cannot replace {rel}: {output}"
    rc, _ = run_git_command(["cat-file", "-e", f"{source}:{rel}"], repo_path, check=False)
    if rc != 0:
        return None  # nothing recorded there yet; the removal stands
    rc, output = run_git_command(
        ["read-tree", f"--prefix={rel}/", f"{source}:{rel}"],
        repo_path, check=False, extra_env=env,
    )
    return None if rc == 0 else f"cannot copy {rel}: {output}"


def publish_overlay_commit(
    repo_path: Path, *, base: str, message: str,
    blobs: Optional[dict] = None, subtrees: Optional[dict] = None,
    label: str = "work repo",
) -> tuple[str, str, str]:
    """Build a commit on *base* carrying the overlay, and push it once.

    *blobs* maps a work-repo-relative path to its complete new text.
    *subtrees* maps a work-repo-relative DIRECTORY to a commit whose copy of
    that directory replaces it — this session's own record, taken from the
    snapshot it just committed, so what is published is exactly what the local
    branch already holds and the next ordinary rebase drops as applied.

    Returns ``(outcome, detail, commit)`` in the vocabulary of
    :func:`claim_push_once`. On any outcome the checkout is exactly as it was.
    """
    destination = _publication_destination(repo_path, label)
    if destination is None:
        return CLAIM_PUSH_ERROR, "no upstream publication destination", ""
    remote, ref = destination
    with _publication_index(repo_path) as env:
        rc, output = run_git_command(["read-tree", base], repo_path, check=False, extra_env=env)
        if rc != 0:
            return CLAIM_PUSH_ERROR, f"cannot seed the publication index: {output}", ""
        for rel, source in (subtrees or {}).items():
            detail = _replace_subtree(repo_path, env, rel, source)
            if detail:
                return CLAIM_PUSH_ERROR, detail, ""
        for rel, text in (blobs or {}).items():
            rc, written = _git_raw(
                repo_path, ["hash-object", "-w", "--stdin", "--path", rel],
                stdin=text.encode("utf-8"),
            )
            if rc != 0:
                return CLAIM_PUSH_ERROR, f"cannot write {rel}: {written.decode(errors='replace')}", ""
            rc, output = run_git_command(
                ["update-index", "--add", "--cacheinfo",
                 f"100644,{written.decode().strip()},{rel}"],
                repo_path, check=False, extra_env=env,
            )
            if rc != 0:
                return CLAIM_PUSH_ERROR, f"cannot stage {rel}: {output}", ""
        rc, tree = run_git_command(["write-tree"], repo_path, check=False, extra_env=env)
        if rc != 0:
            return CLAIM_PUSH_ERROR, f"cannot write the publication tree: {tree}", ""
    rc, commit = run_git_command(
        ["commit-tree", tree.strip(), "-p", base, "-m", message], repo_path, check=False
    )
    if rc != 0:
        return CLAIM_PUSH_ERROR, f"cannot create the publication commit: {commit}", ""
    commit = commit.strip()
    rc, output = run_git_command(["push", remote, f"{commit}:{ref}"], repo_path, check=False)
    if rc == 0:
        return CLAIM_PUSH_WON, output, commit
    if any(marker in output.lower() for marker in _NON_FAST_FORWARD_MARKERS):
        return CLAIM_PUSH_LOST_RACE, output, commit
    print(f"❌ git push failed ({label}): {_scrub_credentials(output)}", file=sys.stderr)
    return CLAIM_PUSH_ERROR, output, commit


def _stash_head(repo_path: Path, label: str) -> tuple[int, str]:
    rc, output = run_git_command(
        ["rev-parse", "--verify", "--quiet", "refs/stash"], repo_path, check=False
    )
    if rc == 1:
        return 0, ""
    if rc != 0:
        return _git_failure("stash inspection", label, rc, output), ""
    return 0, output.strip()


def _rebase_in_progress(repo_path: Path) -> bool:
    """Whether a rebase is paused in *repo_path* — quietly, with no failure line."""
    rc, output = run_git_command(["rev-parse", "--absolute-git-dir"], repo_path, check=False)
    if rc != 0 or not output.strip():
        return False
    git_dir = Path(output.strip())
    return any((git_dir / name).exists() for name in ("rebase-merge", "rebase-apply"))


def _is_index_rel(repo_path: Path, rel: str) -> bool:
    """Whether *rel* is a project README this runtime regenerates."""
    readme = project_index.README_NAME
    return rel in {f"{project}/{readme}" for project in _index_project_rels(repo_path)}


def _index_stage(repo_path: Path, stage: int, rel: str) -> Optional[str]:
    """The text of *rel* at index *stage* during a paused rebase — 2 is
    upstream, 3 the commit being replayed — or ``None`` when that stage is
    missing (a delete/modify conflict) or the blob is not UTF-8 text."""
    rc, data = _git_raw(repo_path, ["show", f":{stage}:{rel}"])
    if rc != 0:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _regenerate_conflicted_readme(repo_path: Path, rel: str) -> bool:
    """Resolve one conflicted project README inside the paused rebase.

    Only a conflict confined to the generated block is this command's to
    resolve: both stages must exist, both must carry well-formed markers, the
    text outside the markers must be identical on both sides (a human's
    conflict in the prose is a human's to merge) and upstream's block must
    not be of a newer format (a newer runtime wrote it; this one must not
    downgrade it). The resolution keeps upstream's hand text and renders the
    block again from the merged tree — the index holds upstream's records
    and this commit's own, and the tracked filter keeps an untracked sibling
    that remains visible in the worktree out — then stages the file.
    """
    upstream = _index_stage(repo_path, 2, rel)
    ours = _index_stage(repo_path, 3, rel)
    if upstream is None or ours is None:
        return False
    try:
        token = project_index.block_format(project_index.extract_block(upstream))
        project_index.extract_block(ours)
        same_hand_text = project_index.strip_block(upstream) == project_index.strip_block(ours)
    except project_index.MalformedMarkers:
        return False
    if token is not None and token > project_index.FORMAT:
        return False
    if not same_hand_text:
        return False
    project = rel[: -(len(project_index.README_NAME) + 1)]
    include = project_index.tracked_record_filter(repo_path, project)
    block = project_index.render_project_block(
        project_index.scan_project(Path(repo_path) / project, include=include, project_rel=project)
    )
    text = project_index.splice_block(upstream, block, heading=_project_heading(project))
    (Path(repo_path) / rel).write_bytes(text.encode("utf-8"))
    rc, _ = run_git_command(["add", "--", rel], repo_path, check=False)
    return rc == 0


def _continue_past_index_conflicts(repo_path: Path, label: str) -> bool:
    """Finish a paused rebase whose only conflicts are generated README blocks.

    The normal path for concurrent writers in one project: two new rows
    under the header, or a new row beside the previously newest row's edit,
    are adjacent hunks and conflict. Each round regenerates every unmerged
    README (:func:`_regenerate_conflicted_readme`) and runs ``rebase
    --continue`` with the editors pinned; a replay that regenerates to
    upstream's bytes is dropped by ``--continue`` itself (git ≥ 2.26, merge
    backend). Another of our unpushed commits stopping on the README starts
    the next round, bounded by :data:`INDEX_CONFLICT_ROUNDS`.

    ``True`` when the rebase completed with only index files regenerated.
    ``False`` — so the caller's ``❌`` line and ``rebase --abort`` run exactly
    as before — for any unmerged path that is not a project README, a
    missing stage, malformed markers on either side, a newer-format upstream
    block, hand text that differs, a ``rebase --continue`` that fails for
    any other reason, the bound exhausted, or anything raising. *label*
    names the repository as every publication helper is told it; this one
    prints nothing of its own, because the caller already names the failure.
    """
    try:
        for _round in range(INDEX_CONFLICT_ROUNDS):
            rc, output = run_git_command(
                ["diff", "--name-only", "-z", "--diff-filter=U"], repo_path, check=False
            )
            if rc != 0:
                return False
            unmerged = [rel for rel in output.split("\0") if rel]
            if not unmerged or not all(_is_index_rel(repo_path, rel) for rel in unmerged):
                return False
            for rel in unmerged:
                if not _regenerate_conflicted_readme(repo_path, rel):
                    return False
            rc, output = run_git_command(
                ["-c", "core.editor=true", "-c", "sequence.editor=true", "rebase", "--continue"],
                repo_path, check=False,
            )
            if rc == 0:
                return not _rebase_in_progress(repo_path)
            if not _rebase_in_progress(repo_path):
                return False
        return False
    except Exception:
        return False


def _push_with_rebase_retries(repo_path: Path, label: str) -> int:
    """Publish existing commits for work, napkin, and pending-commit retries.

    Retain local commits on rebase failure. Return 0 only after fresh remote
    ancestry verification; otherwise return a nonzero Git or validation code.
    """
    destination = _publication_destination(repo_path, label)
    if destination is None:
        return 1
    remote, ref = destination
    for attempt in range(1, PUSH_RETRIES + 1):
        rc = check_commit_ready(repo_path, label)
        if rc != 0:
            return rc
        rc, stash_before = _stash_head(repo_path, label)
        if rc != 0:
            return rc
        rc, output = run_git_command(
            ["pull", "--rebase", "--autostash", remote, ref], repo_path, check=False
        )
        if rc != 0:
            # The normal concurrent path first: another session landed in the
            # same project and the only conflict is the generated README
            # block. Then, and only then, the failure line and the abort.
            if _rebase_in_progress(repo_path) and _continue_past_index_conflicts(repo_path, label):
                print(INDEX_RESOLVED)
            else:
                _git_failure("git pull --rebase", label, rc, output)
                git_dir = _git_directory(repo_path, label)
                if git_dir and any(
                    (git_dir / p).exists() for p in ("rebase-merge", "rebase-apply")
                ):
                    arc, detail = run_git_command(["rebase", "--abort"], repo_path, check=False)
                    if arc != 0:
                        _git_failure("git rebase --abort", label, arc, detail)
                    else:
                        print(
                            "Rebase aborted; local commits retained. "
                            "Resolve the conflict before retrying.", file=sys.stderr,
                        )
                return rc
        # Git can return zero even when applying the autostash failed.
        rc, stash_after = _stash_head(repo_path, label)
        if rc != 0:
            return rc
        if stash_after and stash_after != stash_before:
            return _git_failure(
                "autostash restoration", label, 1,
                "refs/stash changed during pull; automatic restoration was not confirmed. "
                f"Saved edits are available at {stash_after} (refs/stash). "
                f"Inspect git status and git stash show -p {stash_after} before restoring. "
                "Resolve any unmerged paths before retrying publication.",
            )
        rc = check_commit_ready(repo_path, label)
        if rc != 0:
            return rc
        rc, commit = run_git_command(["rev-parse", "HEAD"], repo_path, check=False)
        if rc != 0:
            return _git_failure("git rev-parse HEAD", label, rc, commit)
        rc, output = run_git_command(["push", remote, f"HEAD:{ref}"], repo_path, check=False)
        if rc == 0:
            return _verify_published(repo_path, label, commit.strip(), destination)
        print(
            f"⚠️  git push rejected (attempt {attempt}/{PUSH_RETRIES}): "
            f"{_scrub_credentials(output)}",
            file=sys.stderr,
        )

    print(
        f"❌ git push failed after {PUSH_RETRIES} attempts ({label}): "
        f"{_scrub_credentials(output)}",
        file=sys.stderr,
    )
    return rc


def claim_push_once(repo_path: Path, label: str = "work repo") -> tuple[str, str]:
    """Fetch, then issue ONE plain ``git push`` — the claim-by-push CAS leg.

    The git-level arbitration path for the single-flight release claim
    (docs/RUN-STATE.md §7). Deliberately NOT :func:`_push_with_rebase_retries`:
    that path reacts to a rejected push by rebasing the local commit onto the
    new remote head and pushing again — which would silently stack two
    competing claim commits on top of each other (last writer wins; both
    launches proceed). Here the server's non-fast-forward rejection IS the
    answer: the remote advanced between the caller's fetch-and-evaluate and
    this push, so the claim commit must not land until the new head has been
    re-checked for a foreign claim. The bounded re-fetch/re-evaluate loop
    lives in the caller, never in this helper.

    The commit to push must already exist locally. The fetch up front makes
    an unreachable remote read as ERROR before any push is attempted — a
    release must fail closed, not mistake a dead remote for a lost race.

    Returns:
        ``(outcome, detail)`` where outcome is one of
        :data:`CLAIM_PUSH_WON` (push fast-forwarded the remote — the claim
        landed atomically), :data:`CLAIM_PUSH_LOST_RACE` (non-fast-forward
        rejection — the remote moved; re-fetch and re-evaluate before any
        retry), or :data:`CLAIM_PUSH_ERROR` (fetch or push failed for any
        other reason — transport, auth, missing remote; callers fail
        closed). ``detail`` is git's output for the deciding command.
    """
    rc, output = run_git_command(["fetch"], repo_path, check=False)
    if rc != 0:
        print(f"❌ git fetch failed ({label}): {_scrub_credentials(output)}", file=sys.stderr)
        return CLAIM_PUSH_ERROR, output

    rc, output = run_git_command(["push"], repo_path, check=False)
    if rc == 0:
        return CLAIM_PUSH_WON, output

    lowered = output.lower()
    if any(marker in lowered for marker in _NON_FAST_FORWARD_MARKERS):
        return CLAIM_PUSH_LOST_RACE, output

    print(f"❌ git push failed ({label}): {_scrub_credentials(output)}", file=sys.stderr)
    return CLAIM_PUSH_ERROR, output


def _rel_to_root(path: Optional[Path], root: Path) -> Optional[str]:
    """*path* spelled relative to the work-repo root, or None.

    None for a path outside the root (and for no path at all), which stages
    nothing rather than a string a commit would take literally — the rule
    :func:`run_state.run_rel_for_dir_name` and :func:`specs_index.specs_rel_path`
    already follow for the areas they own.
    """
    if path is None:
        return None
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return None


def _memory_rel_paths() -> list[str]:
    """Both memory scopes, from the module that owns them.

    Imported at call time, not at module import: ``work_repo.memory`` imports
    this module, so a top-level import here would close the cycle (the same
    local-import convention ``specs_index`` uses for ``run_state``).
    """
    from .memory import memory_write_rel_paths  # local: memory imports this module

    return memory_write_rel_paths()


def commit_work_changes(commit_message: Optional[str] = None) -> int:
    """
    Commit and push everything this session writes into the work repo.

    The batching commit behind ``work commit``. Every path is asked of the
    helper that OWNS it rather than composed from the ``{host}/{project}``
    env trio, so the list follows the FORM of the tree (issue #353 D20) and
    can never name a directory the writers do not use.

    Both forms stage the run record (:func:`run_state.run_rel_path_candidates`
    — the resolved dir plus the address a rename vacated) and the specs index
    (:func:`specs_index.specs_rel_path`). The two ends differ:

    * a LEGACY tree also stages ``{host}/{project}/{task}/{target}``, where
      log and report output predating the run-dir unification (issue #87 D4)
      may still sit. Schema form has no such directory — the record is a
      session dir under ``_runs/<slug>/`` (D22) and nothing writes a
      task-target path — so staging one there would name a directory that
      does not exist.
    * a SCHEMA tree also stages both memory scopes
      (:func:`memory.memory_write_rel_paths`). D17 splits memory in two and
      the instance scope lives at the plexus ROOT, outside every project
      dir, so no other path in this list can reach it. That scope is shared,
      so this can carry another launch's freshly persisted memory along —
      the same many-concurrent-writers property the run area already has,
      and pushing it beats losing it when an ephemeral container exits. A
      legacy tree has ONE scope and keeps staging exactly what it staged
      before: memory there is ``work memory persist``'s own push.

    Refuses (1) when nothing resolves a project dir — an unset launch context
    in a legacy tree, an unreadable manifest in a schema one — naming the
    reason it actually has (:func:`~work_repo.utils.no_run_context_error`). A broken registry
    is NOT that case: it resolves the host bucket (D9) and the commit
    proceeds there.

    Args:
        commit_message: Optional commit message (defaults to auto-generated)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    layout = work_repo_layout()
    if layout.base is None:
        print(no_run_context_error(), file=sys.stderr)
        return 1

    paths: list[str] = []

    # Legacy task-target dir: log/report output predating the run-dir
    # unification (issue #87 D4) may still sit here — keep staging it, in the
    # form that has it. `task_target_dir` applies the same LMER_TASK /
    # LMER_TASK_TARGET defaults and the same `sanitize_task_target` this used
    # to apply inline, so the legacy string is unchanged.
    if not layout.is_schema:
        legacy_target = _rel_to_root(task_target_dir(), layout.root)
        if legacy_target is not None:
            paths.append(legacy_target)

    # The run dir itself: the resolved (possibly renamed) dir plus the
    # bare-slug dir, so a rename whose own push failed still gets its old
    # path's deletions staged (commit_work_path skips clean paths). In schema
    # form the bare address is the `_runs/<slug>/` group, which holds every
    # session of the run.
    for rel in run_rel_path_candidates():
        if rel not in paths:
            paths.append(rel)

    # Records this launch seeded into another group (D13): `work seed` writes
    # a record whose own address is not this launch's, so without this the
    # seed is reported as a stray and never pushed (#535). Schema form only;
    # the helper returns [] elsewhere.
    for rel in seeded_record_rel_paths():
        if rel not in paths:
            paths.append(rel)

    # The specs index (issue #101): entries created by the masterplan sync,
    # `work specs-index --rebuild`, and the freeze-rename re-point all land
    # here — without this, only `work artifact` ever pushed them.
    specs_rel = specs_rel_path()
    if specs_rel and specs_rel not in paths:
        paths.append(specs_rel)

    # Both memory scopes (D17), schema form only — see the docstring.
    if layout.is_schema:
        for rel in _memory_rel_paths():
            if rel not in paths:
                paths.append(rel)

    return commit_work_path(paths, commit_message)


def commit_napkin_if_subdir(commit_message: Optional[str] = None) -> int:
    """
    Commit and push the napkin subdir when napkin lives *inside* the work repo.

    The inverse of :func:`push_napkin_if_separate`: in subdir mode
    ``LMER_NAPKIN_PATH`` resolves under ``LMER_WORK_REPO_PATH``, but the
    work-repo commit stages only the paths this session's own work lands in
    (:func:`commit_work_changes`) — napkin is not one of them in either form,
    and it sits at the work-repo root, so without this
    step notes written there are never committed and are silently lost when
    an ephemeral container exits. Stages and pushes just the napkin subdir
    via :func:`commit_work_path`.

    Returns 0 when inapplicable or publication is verified; otherwise nonzero.
    A clean applicable path still checks the remote. Callers warn on failure;
    napkin capture must never block the worklog commit.
    """
    napkin_path_str = os.environ.get("LMER_NAPKIN_PATH")
    if not napkin_path_str:
        return 0
    napkin_path = Path(napkin_path_str).resolve()
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work")).resolve()

    # Subdir mode only: strictly under the work repo and not a git repo of
    # its own (a nested repo would be unreachable via the work repo's index).
    if work_repo_path not in napkin_path.parents:
        return 0
    if (napkin_path / ".git").exists():
        return 0
    if not napkin_path.exists():
        return 0  # no notes were ever written

    rel_path = napkin_path.relative_to(work_repo_path)
    return commit_work_path([str(rel_path)], commit_message or "Update napkin notes")


def report_uncommitted_work_items() -> int:
    """Flag items left uncommitted in the work repo after a ``work commit``.

    ``work commit`` stages only the paths :func:`commit_work_changes` composes
    for this session's form, so a file added elsewhere — e.g. a new
    ``{host}/{project}/info/*.md`` — is silently left behind (issue #85). This
    runs a repo-wide ``git status --porcelain --untracked-files=all`` and prints
    one ⚠️ block naming the untracked/unstaged entries so the user notices them.
    ``--untracked-files=all`` lists files individually rather than collapsing a
    brand-new directory into a single entry, so the count is per-file and every
    stray file is named. The list is capped at :data:`UNTRACKED_REPORT_CAP`
    entries with a "... and N more" tail so a large dirty tree cannot flood the
    output. Nothing is printed when the tree is clean.

    Fail-soft by contract: this is a reminder, never a gate. It never raises
    and returns 0 on any error (or when the work repo is absent), so a caller
    can invoke it after a commit without risk to that commit's exit code.

    Returns:
        The number of uncommitted entries found (0 when clean or on error).
    """
    try:
        work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
        if not work_repo_path.exists():
            return 0
        rc, output = run_git_command(
            ["status", "--porcelain", "--untracked-files=all"],
            work_repo_path,
            check=False,
        )
        if rc != 0:
            return 0
        entries = [line for line in output.splitlines() if line.strip()]
        if not entries:
            return 0

        print(
            f"⚠️  {len(entries)} uncommitted item(s) remain in the work repo "
            "(not staged by `work commit`):"
        )
        for line in entries[:UNTRACKED_REPORT_CAP]:
            print(f"   {line}")
        remaining = len(entries) - UNTRACKED_REPORT_CAP
        if remaining > 0:
            print(f"   ... and {remaining} more")
        print(
            "   These fell outside the paths `work commit` stages — add & "
            "commit them manually if they should be kept."
        )
        return len(entries)
    except Exception:
        # Reminder only: a status hiccup must never affect the commit result.
        return 0


def push_napkin_if_separate(commit_message: Optional[str] = None) -> int:
    """
    Commit and push the napkin repo when it is a *separate* git repo.

    Napkin is pushed only when ``LMER_NAPKIN_PATH`` resolves to a git repo that
    lives *outside* ``LMER_WORK_REPO_PATH``. In subdir mode (napkin under the
    work repo) the work-repo commit already captures it, so this is a no-op.

    Uses the same commit-first ordering and verified publication as
    :func:`commit_work_path`. Returns 0 when inapplicable or publication is
    verified, including clean retries; missing upstream and network errors
    return nonzero.
    Callers treat any failure as a warning — napkin push must never block
    the worklog commit.
    """
    napkin_path_str = os.environ.get("LMER_NAPKIN_PATH")
    if not napkin_path_str:
        return 0
    napkin_path = Path(napkin_path_str).resolve()
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work")).resolve()

    # Separate-repo mode only: a git repo that is not the work repo or under it.
    if not (napkin_path / ".git").exists():
        return 0
    if napkin_path == work_repo_path or work_repo_path in napkin_path.parents:
        return 0

    rc = check_commit_ready(napkin_path, "napkin")
    if rc != 0:
        return rc

    print("➕ Staging all changes in napkin repository...")
    rc, status_output = _stage_and_check(napkin_path, [], "napkin")
    if rc != 0:
        return rc
    if not status_output.strip():
        print("No new file changes to commit in napkin repository")
        return _push_pending_commits(napkin_path, "napkin")

    # 2. Commit
    if commit_message is None:
        commit_message = "Update napkin notes"
    print("💾 Committing changes to napkin repository...")
    rc, output = run_git_command(["commit", "-m", commit_message], napkin_path, check=False)
    if rc != 0:
        print(f"⚠️  git commit failed (napkin): {_scrub_credentials(output)}", file=sys.stderr)
        return rc

    # 3. Integrate the remote and push, rebasing between attempts.
    print("📤 Pushing changes to napkin repository...")
    rc = _push_with_rebase_retries(napkin_path, "napkin")
    if rc == 0:
        print("✅ Successfully committed and pushed changes to napkin repository")
    return rc
