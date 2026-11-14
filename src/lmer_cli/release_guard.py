"""``lmer-release-guard``: the release flow's leak scan and pre-push guard.

docs/RELEASE-GUARD.md is the reference: the verbs, the detectors, the
project-tier ``release.leak_scan`` block, and where a release run uses each.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

import yaml

from work_repo import utils as work_utils

# The operator's call on a manifest with no ``release.leak_scan`` block: a hard
# stop. Flipping this to False makes such a manifest scan with the generic
# detectors only.
REQUIRE_LEAK_SCAN_BLOCK = True

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FINDING = 2

LIVE_CATEGORY = "live-secret"
GUARD_CATEGORY = "guard-string"
# Hits in these categories are findings by definition, not triage material.
FINDING_CATEGORIES = (LIVE_CATEGORY, GUARD_CATEGORY)

# Project-private detectors (handles, internal hosts) never live here: this
# file ships publicly. They come from the project tier's `patterns`.
GENERIC_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("token-prefix", work_utils._SECRET_VALUE_PREFIX_RE),
    ("token-shape", work_utils._TOKEN_SHAPE_RE),
    ("url-userinfo", re.compile(r"://[^/\s@\"'<>]+@")),
    ("secret-assign", re.compile(
        r"(?i)(?:api[_-]?key|apikey|secret|password|passwd|pwd|token)\s*=\s*[\"'][^\"']+[\"']")),
    ("aws-keyid", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("pem-private", re.compile(r"BEGIN [A-Z0-9 ]*PRIVATE KEY")),
    ("aws-secret", re.compile(r"(?i)aws_(?:secret_access_key|access_key_id)\s*[=:]\s*\S+")),
    ("openssh-body", re.compile(r"b3BlbnNzaC1rZXktdjE")),
    ("slack-webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("google-api", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*")),
    ("stripe", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9a-zA-Z]{10,}")),
    ("npm", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi", re.compile(r"pypi-AgE[A-Za-z0-9_\-]{20,}")),
    ("matrix", re.compile(r"\b(?:syt|mat|mct)_[A-Za-z0-9_]{16,}")),
    ("env-secret-assign", re.compile(
        r"(?:^|\s|export\s+)[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIALS?)"
        r"[A-Z0-9_]*=(?![\s\"']*\$)[^\s\"'<>]{6,}")),
    ("yaml-json-secret", re.compile(
        r"(?i)[\"']?\b(?:token|secret|password|passwd|api[_-]?key|apikey|access[_-]?key|private[_-]?key"
        r"|client[_-]?secret)[\"']?\s*:\s*[\"']?(?![\s\"']*[\$<{])[^\s\"',}{\[\]()]{6,}")),
    ("auth-header", re.compile(
        r"(?i)\b(?:authorization|private-token|x-api-key)[\"']?\s*[:=]\s*[\"']?(?:bearer|token|basic)?\s*"
        r"(?![\$<{])[A-Za-z0-9._\-+/=]{12,}")),
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?<![\w.:/-])(?:\+\d{1,3}[\s.\-])?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\w.-])")),
    ("internal-host", re.compile(
        r"(?i)\b[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.(?:internal|corp|lan|intranet|home\.arpa)\b")),
    ("rfc1918", re.compile(
        r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3})(?![\d.])")),
    ("home-path", re.compile(r"(?:/home/[A-Za-z0-9_.\-]+|/Users/[A-Za-z0-9_.\-]+)")),
)

# The only generic detectors searched in a binary file's raw bytes; the others
# would match noise there.
BINARY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (cat, rx) for cat, rx in GENERIC_PATTERNS if cat in ("token-prefix", "token-shape"))

_HOST_RE = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}\b")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
# Explicit prefixes, rename detection and no textconv or external driver:
# user config (diff.noprefix, diff.renames, a diff.<driver>.textconv named by
# a committed .gitattributes) must not change what the parser sees.
_DIFF_ARGS = ("-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "-M",
              "--src-prefix=a/", "--dst-prefix=b/")
_PATTERN_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_LEAK_SCAN_KEYS = frozenset({"public_mirror", "exceptions", "ancestry_denylist", "patterns", "guard_strings"})


class GuardError(Exception):
    """A config, git or usage problem: the verb could not give a verdict."""


@dataclass(frozen=True)
class LeakScanConfig:
    public_mirror: bool
    exceptions: Optional[str] = None
    ancestry_denylist: tuple[str, ...] = ()
    patterns: tuple[tuple[str, re.Pattern], ...] = ()
    guard_strings: tuple[re.Pattern, ...] = ()


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys: the last one would
    otherwise win silently, and a second ``leak_scan:`` can opt a project
    out unseen."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _list(block: dict, key: str) -> list:
    if key not in block:
        return []
    value = block[key]
    if not isinstance(value, list):
        raise GuardError(f"release.leak_scan.{key} must be a list")
    return value


def _compile(regex, where: str, flags: int = 0) -> re.Pattern:
    if not isinstance(regex, str) or not regex:
        raise GuardError(f"{where} must be a non-empty regex string")
    try:
        return re.compile(regex, flags)
    except re.error as exc:
        raise GuardError(f"{where} is not a valid regex: {exc}") from None


def parse_leak_scan(block, source: str) -> LeakScanConfig:
    """Validate a ``release.leak_scan`` mapping. Unknown keys are refused so a
    misspelt key cannot silently drop a check."""
    if not isinstance(block, dict):
        raise GuardError(f"release.leak_scan in {source} must be a mapping")
    unknown = sorted(set(block) - _LEAK_SCAN_KEYS)
    if unknown:
        raise GuardError(f"release.leak_scan in {source} has unknown key(s): {', '.join(unknown)}")
    public_mirror = block.get("public_mirror")
    if not isinstance(public_mirror, bool):
        raise GuardError(f"release.leak_scan.public_mirror in {source} must be exactly true or false")

    exceptions = block.get("exceptions")
    if exceptions is not None and (not isinstance(exceptions, str) or not exceptions.strip()):
        raise GuardError("release.leak_scan.exceptions must be a non-empty file name")

    denylist = _list(block, "ancestry_denylist")
    for sha in denylist:
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise GuardError(f"release.leak_scan.ancestry_denylist entry {sha!r} is not a full 40-hex SHA")

    raw_patterns = _list(block, "patterns")
    taken = {name for name, _ in GENERIC_PATTERNS} | set(FINDING_CATEGORIES)
    patterns = []
    for i, entry in enumerate(raw_patterns):
        where = f"release.leak_scan.patterns[{i}]"
        if not isinstance(entry, dict) or set(entry) != {"name", "regex"}:
            raise GuardError(f"{where} must be a mapping with exactly `name` and `regex`")
        name = entry["name"]
        if not isinstance(name, str) or not _PATTERN_NAME_RE.fullmatch(name):
            raise GuardError(f"{where}.name must be lowercase letters, digits and hyphens")
        if name in taken:
            raise GuardError(f"{where}.name {name!r} is already in use")
        taken.add(name)
        patterns.append((name, _compile(entry["regex"], f"{where}.regex")))

    raw_guard = _list(block, "guard_strings")
    guard = tuple(
        _compile(regex, f"release.leak_scan.guard_strings[{i}]", re.IGNORECASE)
        for i, regex in enumerate(raw_guard)
    )
    return LeakScanConfig(
        public_mirror=public_mirror,
        exceptions=exceptions,
        ancestry_denylist=tuple(denylist),
        patterns=tuple(patterns),
        guard_strings=guard,
    )


def load_config(path: Path) -> LeakScanConfig:
    """Read the ``release.leak_scan`` block from a resolved release ``task.yaml``."""
    try:
        data = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise GuardError(f"cannot read release manifest {path}: {exc}") from None
    release = data.get("release") if isinstance(data, dict) else None
    if not isinstance(release, dict):
        raise GuardError(f"{path} has no `release:` mapping")
    if "leak_scan" not in release:
        if REQUIRE_LEAK_SCAN_BLOCK:
            raise GuardError(
                f"release.leak_scan is not set in {path}; the project tier must declare it "
                "(`public_mirror: false` is the opt-out)"
            )
        return LeakScanConfig(public_mirror=True)
    return parse_leak_scan(release["leak_scan"], str(path))


def mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 6:
        return f"{value[:1]}…({len(value)})"
    return f"{value[:3]}…{value[-2:]}({len(value)})"


def live_secrets(repo: Path) -> list[str]:
    """Secret values this session holds: secret-named env vars, the password
    half of git credential-store lines, and the repo's gitignored LOCAL/ env
    files. Longest first, so a value is reported once, not by its substrings."""
    values = set(work_utils._collect_secret_values())
    home = Path(os.path.expanduser("~"))
    try:
        cred_files = [p for p in home.iterdir() if p.name.startswith(".git-credentials")]
    except OSError:
        cred_files = []
    for cred in cred_files:
        try:
            for line in cred.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"\w+://[^:/]+:([^@]+)@", line.strip())
                if m and len(m.group(1)) >= work_utils._MIN_SECRET_LENGTH:
                    values.add(m.group(1))
        except OSError:
            pass
    local = repo / "LOCAL"
    if local.is_dir():
        for env_file in local.iterdir():
            try:
                lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if "=" in line:
                    value = line.split("=", 1)[1].strip().strip("\"'")
                    if len(value) >= work_utils._MIN_SECRET_LENGTH:
                        values.add(value)
    return sorted(values, key=len, reverse=True)


class Scanner:
    def __init__(self, config: LeakScanConfig, secrets: list[str]):
        self.project_detectors = (config.patterns
                                  + tuple((GUARD_CATEGORY, rx) for rx in config.guard_strings))
        self.detectors = GENERIC_PATTERNS + self.project_detectors
        self.secrets = secrets

    def _live_hits(self, where: str, lineno: int, text: str) -> list[dict]:
        # A live value gets no prefix or suffix in the output.
        return [{"file": where, "line": lineno, "cat": LIVE_CATEGORY,
                 "masked": f"live secret #{index + 1} ({len(secret)})"}
                for index, secret in enumerate(self.secrets) if secret in text]

    def _private_host(self, host: str) -> bool:
        """A host the inventory must not print whole: one a held value, a
        project pattern or a guard string already reports, masked, as a hit."""
        return (any(host in s.lower() or s.lower() in host for s in self.secrets)
                or any(rx.search(host) for _, rx in self.project_detectors))

    def scan(self, lines: Iterable[tuple[str, int, str]]) -> dict:
        hits = []
        hosts: Counter = Counter()
        count = 0
        for where, lineno, text in lines:
            count += 1
            hits.extend(self._live_hits(where, lineno, text))
            for cat, rx in self.detectors:
                for m in rx.finditer(text):
                    hits.append({"file": where, "line": lineno, "cat": cat, "masked": mask(m.group(0))})
            for m in _HOST_RE.finditer(text):
                hosts[m.group(0).lower()] += 1
        hosts = {h: n for h, n in hosts.items() if not self._private_host(h)}
        return {"lines": count, "hits": hits, "hosts": hosts}

    def scan_binary(self, where: str, data: bytes) -> list[dict]:
        """Held values, token shapes, project patterns and guard strings in a
        binary file's bytes. Latin-1 maps each byte to one character, so a
        held value matches exactly when its UTF-8 bytes are present."""
        text = data.decode("latin-1")
        secrets = [s.encode("utf-8").decode("latin-1") for s in self.secrets]
        hits = [{"file": where, "line": 0, "cat": LIVE_CATEGORY,
                 "masked": f"live secret #{index + 1} ({len(secret)})"}
                for index, (secret, raw) in enumerate(zip(self.secrets, secrets)) if raw in text]
        for cat, rx in BINARY_PATTERNS + self.project_detectors:
            for m in rx.finditer(text):
                hits.append({"file": where, "line": 0, "cat": cat, "masked": mask(m.group(0))})
        return hits


def _git(repo: Path, *args: str, ok_codes=(0,)) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
        capture_output=True,
    )
    if proc.returncode not in ok_codes:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GuardError(f"git {' '.join(args)} failed ({proc.returncode}): {err}")
    return proc


def _git_out(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.decode("utf-8", "replace")


def _header_path(raw: str, prefix: str) -> Optional[str]:
    """The path in a ``--- a/x`` / ``+++ b/x`` header, C-unquoting the form
    git uses for names with quotes, backslashes or control characters."""
    path = raw[4:].rstrip("\t")
    if path.startswith('"') and path.endswith('"'):
        path = codecs.escape_decode(path[1:-1].encode("utf-8"))[0].decode("utf-8", "replace")
    return path[len(prefix):] if path.startswith(prefix) else None


def diff_lines(diff_text: str) -> tuple[list, list]:
    """(added, removed) as (file, line, text) from a ``-U0`` diff. Header
    lines are only read before a file's first hunk, so a removed line whose
    text starts with ``--`` is never mistaken for one."""
    added, removed = [], []
    new_file = old_file = None
    in_header = False
    new_ln = old_ln = 0
    # Only "\n" ends a diff line: splitlines() would also break at \r, \f, \v
    # and the Unicode separators, and drop the fragment after one unscanned.
    for raw in diff_text.split("\n"):
        if raw.startswith("diff --git "):
            in_header, new_file, old_file = True, None, None
            continue
        if in_header and not raw.startswith("@@"):
            if raw.startswith("+++ "):
                new_file = _header_path(raw, "b/")
            elif raw.startswith("--- "):
                old_file = _header_path(raw, "a/")
            continue
        if raw.startswith("@@"):
            in_header = False
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", raw)
            old_ln, new_ln = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        elif raw.startswith("+") and new_file is not None:
            added.append((new_file, new_ln, raw[1:]))
            new_ln += 1
        elif raw.startswith("-") and old_file is not None:
            removed.append((old_file, old_ln, raw[1:]))
            old_ln += 1
    return added, removed


def _numstat(repo: Path, a: str, b: str) -> Iterator[tuple[str, str, str, str]]:
    """(plus, minus, old path, new path) per ``--numstat -z`` row. A rename
    row has an empty path field followed by the old and new paths.

    ``-U0`` as in the parsed diff: git aligns some changes differently with
    no context, so a default-context numstat can count a line more."""
    fields = _git_out(repo, "diff", "--numstat", "-z", "-U0", "--no-textconv", "--no-ext-diff",
                      "--text", "-M", a, b).split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        parts = fields[i].split("\t", 2)
        if len(parts) != 3:
            raise GuardError(f"diff {a[:12]}..{b[:12]}: unexpected numstat row {fields[i]!r}")
        plus, minus, path = parts
        if path:
            yield plus, minus, path, path
            i += 1
        elif i + 2 < len(fields):
            yield plus, minus, fields[i + 1], fields[i + 2]
            i += 3
        else:
            raise GuardError(f"diff {a[:12]}..{b[:12]}: truncated numstat rename row")


def _blob(repo: Path, rev: str, path: str) -> Optional[bytes]:
    proc = _git(repo, "cat-file", "blob", f"{rev}:{path}", ok_codes=(0, 128))
    return proc.stdout if proc.returncode == 0 else None


def _is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def changed_lines(repo: Path, a: str, b: str) -> tuple[list, list, list]:
    """Added and removed lines between two revisions, and the binary files
    on ``b``'s side as (path, bytes), cross-checked against ``--numstat`` so
    a diff the parser could not read fails loudly instead of scanning
    nothing.

    numstat counts no lines for a file git calls binary. One that is really
    text (a ``.gitattributes`` entry can say ``binary``) keeps its lines and
    only leaves the count check; a real binary file leaves the line scan and
    is returned for a byte scan. A binary deletion adds nothing to scan."""
    added, removed = diff_lines(_git_out(repo, "diff", *_DIFF_ARGS, a, b))
    want_added = want_removed = 0
    uncounted_new, uncounted_old, drop_new, drop_old, binary = set(), set(), set(), set(), []
    for plus, minus, old, new in _numstat(repo, a, b):
        if plus == minus == "-":
            uncounted_new.add(new)
            uncounted_old.add(old)
            new_data, old_data = _blob(repo, b, new), _blob(repo, a, old)
            if new_data is not None and not _is_text(new_data):
                drop_new.add(new)
                binary.append((new, new_data))
            if old_data is not None and not _is_text(old_data):
                drop_old.add(old)
        elif plus.isdigit() and minus.isdigit():
            want_added += int(plus)
            want_removed += int(minus)
        else:
            raise GuardError(f"diff {a[:12]}..{b[:12]}: unexpected numstat row {plus!r} {minus!r} {new!r}")
    counted = (sum(1 for ln in added if ln[0] not in uncounted_new),
               sum(1 for ln in removed if ln[0] not in uncounted_old))
    if counted != (want_added, want_removed):
        raise GuardError(
            f"diff {a[:12]}..{b[:12]}: parsed {counted[0]}+/{counted[1]}- lines but git counts "
            f"{want_added}+/{want_removed}-; refusing to report a partial scan")
    return ([ln for ln in added if ln[0] not in drop_new],
            [ln for ln in removed if ln[0] not in drop_old], binary)


def _scan_binary(scanner: Scanner, binary: list) -> dict:
    hits = [hit for path, data in binary for hit in scanner.scan_binary(path, data)]
    return {"files": [path for path, _ in binary], "hits": hits}


def _empty_tree(repo: Path) -> str:
    return _git_out(repo, "hash-object", "-t", "tree", os.devnull).strip()


def scan_commits(repo: Path, published: str, candidate: str, scanner: Scanner) -> list[dict]:
    """Every commit a push of ``candidate`` publishes over ``published``: a
    single-parent commit's own added and removed lines; a merge's tree
    against ``git merge-tree --write-tree`` of its parents, scanning only
    the resolution when they differ."""
    rows = _git_out(repo, "rev-list", "--reverse", "--parents", f"{published}..{candidate}")
    out = []
    for row in rows.splitlines():
        sha, *parents = row.split()
        rec: dict = {"sha": sha, "parents": parents}
        if len(parents) == 2:
            proc = _git(repo, "merge-tree", "--write-tree", *parents, ok_codes=(0, 1))
            merge_tree = proc.stdout.decode().split("\n", 1)[0].strip()
            tree = _git_out(repo, "rev-parse", f"{sha}^{{tree}}").strip()
            rec.update(kind="merge", merge_tree=merge_tree, tree=tree, clean_auto=merge_tree == tree)
            if merge_tree != tree:
                added, _, binary = changed_lines(repo, merge_tree, tree)
                rec.update(added=scanner.scan(added), binary=_scan_binary(scanner, binary))
        else:
            # A root commit is all additions; an octopus merge is scanned whole
            # against its first parent, since merge-tree takes two parents.
            kind = {0: "root", 1: "commit"}.get(len(parents), "octopus")
            base = parents[0] if parents else _empty_tree(repo)
            added, removed, binary = changed_lines(repo, base, sha)
            rec.update(kind=kind, added=scanner.scan(added), removed=scanner.scan(removed),
                       binary=_scan_binary(scanner, binary))
        out.append(rec)
    return out


def scan_messages(repo: Path, published: str, candidate: str, scanner: Scanner) -> dict:
    """Commit messages and author/committer identities in the range."""
    # -z ends each record with a NUL, which no field can contain.
    fields = _git_out(repo, "log", "-z", "--format=%H%x00%an <%ae>%x00%cn <%ce>%x00%B",
                      f"{published}..{candidate}").split("\0")
    if fields[-1] == "":
        fields.pop()
    if len(fields) % 4:
        raise GuardError(f"git log {published}..{candidate}: {len(fields)} fields, not whole records")
    lines = []
    for at in range(0, len(fields), 4):
        sha, author, committer, body = fields[at:at + 4]
        sha = sha.lstrip("\n")
        lines.append((f"{sha[:12]}:author", 0, author))
        lines.append((f"{sha[:12]}:committer", 0, committer))
        lines.extend((f"{sha[:12]}:message", i + 1, text)
                     for i, text in enumerate(body.rstrip("\n").split("\n")))
    return scanner.scan(lines)


def _hits(result: dict) -> Iterator[dict]:
    """Every hit in a scan result, however deeply it is nested."""
    if isinstance(result, dict):
        if "hits" in result and isinstance(result["hits"], list):
            yield from result["hits"]
        for value in result.values():
            if isinstance(value, (dict, list)):
                yield from _hits(value)
    elif isinstance(result, list):
        for item in result:
            yield from _hits(item)


def _summarize(out: dict) -> int:
    counts = Counter(hit["cat"] for hit in _hits(out))
    out["summary"] = dict(sorted(counts.items()))
    for cat, n in sorted(counts.items()):
        print(f"  {cat:<18} {n}")
    if not counts:
        print("  no hits")
    findings = sum(counts.get(cat, 0) for cat in FINDING_CATEGORIES)
    if findings:
        print(f"FINDING: {findings} live-secret or guard-string hit(s); nothing may be published")
        return EXIT_FINDING
    return EXIT_OK


def _write(path: Path, out: dict) -> None:
    Path(path).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def cmd_scan(args) -> int:
    repo = Path(args.repo)
    config = load_config(args.config)
    scanner = Scanner(config, live_secrets(repo))
    published = args.published or args.base
    added, _, binary = changed_lines(repo, args.base, args.candidate)
    out = {
        "verb": "scan",
        "base": _git_out(repo, "rev-parse", args.base).strip(),
        "candidate": _git_out(repo, "rev-parse", args.candidate).strip(),
        "published": _git_out(repo, "rev-parse", published).strip(),
        "live_secret_values": len(scanner.secrets),
        "project_patterns": [name for name, _ in config.patterns],
        "cumulative": scanner.scan(added),
        "cumulative_binary": _scan_binary(scanner, binary),
        "commits": scan_commits(repo, published, args.candidate, scanner),
        "messages": scan_messages(repo, published, args.candidate, scanner),
    }
    print(f"scan {out['base'][:12]}..{out['candidate'][:12]} "
          f"(per commit from {out['published'][:12]}): {len(out['commits'])} commits")
    status = _summarize(out)
    _write(args.out, out)
    return status


def cmd_scan_text(args) -> int:
    config = load_config(args.config)
    scanner = Scanner(config, live_secrets(Path(args.repo)))
    try:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise GuardError(f"cannot read {args.file}: {exc}") from None
    lines = [(str(args.file), i + 1, t) for i, t in enumerate(text.split("\n"))]
    out = {"verb": "scan-text", "file": str(args.file), "live_secret_values": len(scanner.secrets),
           "text": scanner.scan(lines)}
    print(f"scan-text {args.file}: {out['text']['lines']} lines")
    status = _summarize(out)
    _write(args.out, out)
    return status


def allowed_refs(version: str) -> tuple[str, ...]:
    return ("refs/heads/main", f"refs/tags/v{version}", f"refs/heads/release-candidate/v{version}")


def prepush(repo: Path, obj: str, ref: str, version: str, published: str,
            config: LeakScanConfig, secrets: list[str]) -> list[tuple[bool, str]]:
    """The pre-push guard's checks, as (passed, line) pairs."""
    results = []
    if ref in allowed_refs(version):
        results.append((True, f"ref allowed: {ref}"))
    else:
        results.append((False, f"ref not allowed: {ref} (allowed: {', '.join(allowed_refs(version))})"))

    commit = _git_out(repo, "rev-parse", "--verify", f"{obj}^{{commit}}").strip()
    shallow = _git_out(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    for bad in config.ancestry_denylist:
        if shallow:
            results.append((False, f"{bad[:8]} ancestry unprovable: shallow clone"))
        elif _git(repo, "cat-file", "-e", f"{bad}^{{commit}}", ok_codes=(0, 1, 128)).returncode != 0:
            # Every ancestor of a commit in a full clone is present locally.
            results.append((True, f"{bad[:8]} not an ancestor (absent locally)"))
        elif _git(repo, "merge-base", "--is-ancestor", bad, commit, ok_codes=(0, 1)).returncode == 0:
            results.append((False, f"{bad[:8]} IS an ancestor of {commit[:12]}"))
        else:
            results.append((True, f"{bad[:8]} not an ancestor"))

    span = f"{_git_out(repo, 'rev-parse', '--verify', f'{published}^{{commit}}').strip()}..{commit}"
    # remerge-diff shows what a merge itself adds; plain -p shows merges as
    # nothing at all.
    text = _git_out(repo, "log", "-p", "--text", "--no-textconv", "--no-ext-diff", "--diff-merges=remerge",
                    "--format=%an <%ae> %cn <%ce>%n%B", span)
    if _git_out(repo, "cat-file", "-t", obj).strip() == "tag":
        text += "\n" + _git_out(repo, "cat-file", "tag", obj)
    for i, rx in enumerate(config.guard_strings):
        n = len(rx.findall(text))
        results.append((n == 0, f"guard_strings[{i}]: {n} match(es) in {span[:12]}..{commit[:12]}"))
    live = sum(1 for secret in secrets if secret in text)
    results.append((live == 0, f"live secrets: {live} value(s) present in {span[:12]}..{commit[:12]}"))
    return results


def cmd_prepush(args) -> int:
    repo = Path(args.repo)
    config = load_config(args.config)
    results = prepush(repo, args.object, args.ref, args.version, args.published, config, live_secrets(repo))
    for passed, line in results:
        print(f"{'PASS' if passed else 'FAIL'} {line}")
    ok = all(passed for passed, _ in results)
    print("GUARD PASS" if ok else "GUARD FAIL")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_check_config(args) -> int:
    config = load_config(args.config)
    if not config.public_mirror:
        print("public_mirror: false — leak scan and pre-push guard opted out")
        return EXIT_OK
    print("public_mirror: true")
    print(f"exceptions: {config.exceptions or '—'}")
    print(f"ancestry_denylist: {len(config.ancestry_denylist)} commit(s)")
    print(f"patterns: {', '.join(name for name, _ in config.patterns) or '—'}")
    print(f"guard_strings: {len(config.guard_strings)}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lmer-release-guard", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="verb", required=True)

    def common(p):
        p.add_argument("--config", required=True, type=Path,
                       help="the resolved release task.yaml (Phase 0.5's first match)")
        p.add_argument("--repo", default=".", help="repository to scan (default: the current directory)")

    p = sub.add_parser("check-config", help="validate the release.leak_scan block")
    common(p)
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("scan", help="scan everything a push of the candidate publishes")
    common(p)
    p.add_argument("--base", required=True, help="the previous release tag (cumulative diff base)")
    p.add_argument("--candidate", required=True, help="the commit to be published")
    p.add_argument("--published", help="what the public remote already has (default: --base)")
    p.add_argument("--out", required=True, type=Path, help="JSON result file (values masked)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("scan-text", help="scan a text file (a merge message, a public comment)")
    common(p)
    p.add_argument("file", type=Path)
    p.add_argument("--out", required=True, type=Path, help="JSON result file (values masked)")
    p.set_defaults(func=cmd_scan_text)

    p = sub.add_parser("prepush", help="the guard to run before every public push")
    common(p)
    p.add_argument("object", help="the commit or tag being pushed")
    p.add_argument("ref", help="the destination ref, e.g. refs/heads/main")
    p.add_argument("--version", required=True, help="the release version, without the v")
    p.add_argument("--published", required=True, help="the public remote's main, freshly fetched")
    p.set_defaults(func=cmd_prepush)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GuardError as exc:
        print(f"lmer-release-guard: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
