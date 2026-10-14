"""Finding model, check-code registry and strictness-tier engine of the
validator (spec §7, D14; SCHEMA.md "Validator" / "Inventory").

Every check module of ``lmer-schema validate`` plugs into this module and
nothing else: it registers its codes here, emits findings through a
:class:`Report`, and never decides a severity at the call site. This module
owns NO check logic — it is the vocabulary (severity, form, tier), the value
type (:class:`Finding`), the collector (:class:`Report`) and the D14 tier
table.

Exit taxonomy
=============
The validator's exit codes are its own — ``0`` clean (warnings and
informational findings allowed; **warnings never block**, D14), ``1`` one or
more ERROR findings, ``2`` unusable input (no such path, an unreadable or
unparseable manifest). This is the shape of ``work plan check`` and
deliberately NOT the ``0/2/64`` codes of ``lmer_cli.container.sources``.
"Unusable" is a report STATE the caller sets (:meth:`Report.mark_unusable`,
typically from a ``ManifestError``), not a fourth severity: a finding says
something about a path inside the tree, whereas "unusable" says there is no
tree to say anything about. A report marked unusable exits ``2`` whatever
findings it also carries.

Tier engine — decision: a CENTRAL map keyed by code
===================================================
D14 makes some findings tier-sensitive (ERROR in a schema-form tree, WARN or
INFO in a legacy tree) and others not (state-file SHAPE violations are ERROR
"wherever they live"). There were two ways to implement that, and every
later check module depends on which one it registers against:

1. a per-finding attribute — each ``Finding`` declares "I am an integrity
   finding" and the report downgrades any such finding in a legacy tree;
2. a central downgrade map keyed by code — each code is registered ONCE with
   its schema-form severity and, only when D14 makes it tier-sensitive, its
   legacy-tier severity; ``Report.add`` resolves the severity from the map
   and the form.

This module implements **option 2**. The reasons: the severity of a code is
then a property of the code, not of the call site, so two places emitting
the same code cannot disagree and a shape code can never be softened by
accident; the ``--list-checks`` inventory (a later CLI task) is a plain read
of the registry rather than a static analysis of every emit site; and the
D14 table becomes one auditable list (:data:`CHECKS` below), which is what
SCHEMA.md's strictness table is reconciled against. The cost is that a
condition with two severities is two codes — e.g. a registered remote's
host-bucket path as a REAL directory (ERROR) versus as a compat SYMLINK
(INFO) — which is the right shape for a stable machine contract anyway.

What check modules do
---------------------
- Register each code at import time with :func:`register` (or
  ``CHECKS.register``): ``register("index.dangling-link", Severity.ERROR,
  legacy=Severity.WARN, tier=Tier.MACHINE, summary="...")``. ``legacy=`` is
  given ONLY for the D14-downgraded codes and must be strictly less severe.
- Emit with ``report.add(code, path, message, detail=...)``; the report
  knows the tree's form. A host bucket inside a schema-form tree is checked
  at the legacy tier's severities (SCHEMA.md "Outside the schema"), which is
  the one case for the per-call ``form=`` override.
- Never construct a ``Finding`` with a hand-picked severity for a registered
  code; :meth:`Report.append` exists for pre-built findings (the runner's
  "a check raised" finding, a JSON document read back) and does not
  re-resolve.

Code namespace convention
-------------------------
A code is ``<namespace>.<kebab-name>`` (:data:`CODE_RE`), the namespace being
the check module that owns it (:data:`NAMESPACES`): ``manifest.*`` (the
manifest envelope), ``registry.*`` (project entries and remotes), ``tree.*``
(registry-to-directory consistency, host buckets, ``_`` reservation,
archive mirror), ``agents.*`` (the ``_agents/`` surface and memory at both
scopes), ``record.*`` (state files, events, session-unit shape),
``index.*`` (``_specs/``/``_plans/`` symlinks and ``PLANS.md``) and
``runner.*`` (the composing runner's own findings). Registering twice, a
malformed code, or an unknown namespace fails loud at import time so a
collision is a test failure, never a silent overwrite. Codes are a consumed
contract (the hosted connector keys on them): renaming one is a breaking
change of the JSON output.

Pre-registered codes
--------------------
Only the D14 tier table lives here — the codes D14 and SCHEMA.md name by
behaviour: the downgraded integrity codes and their explicit "error in every
form" shape counterparts. The check modules that emit them (agents, record,
index) use these codes rather than re-registering them; everything else is
registered by its own module.

Form
----
:class:`Form` is the two-valued answer of form detection (D20: manifest
present → ``SCHEMA``, absent → ``LEGACY``). This module never sniffs
``_lmer.yaml``; the form is a parameter. ``Form.coerce`` also accepts the
plain strings ``"schema"``/``"legacy"`` (the spelling of
``tests/plexus_fixtures.SCHEMA_FORM``/``LEGACY_FORM``) and any object
carrying one of them as ``.value``, so the form-detection module can return
this enum or its own equivalent without coupling the layers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath, PurePosixPath
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

__all__ = [
    "EXIT_CLEAN",
    "EXIT_ERRORS",
    "EXIT_UNUSABLE",
    "Severity",
    "SEVERITIES",
    "Form",
    "Tier",
    "Finding",
    "CODE_RE",
    "NAMESPACES",
    "CheckCode",
    "CheckRegistry",
    "CHECKS",
    "register",
    "Report",
]

#: The validator's exit taxonomy (§7). Not the sources-CLI codes.
EXIT_CLEAN = 0
EXIT_ERRORS = 1
EXIT_UNUSABLE = 2


class Severity(Enum):
    """Finding severity. Only ERROR affects conformance; WARN and INFO never
    block (D14). ``rank`` is the report order (ERROR first)."""

    ERROR = "ERROR"
    WARN = "WARN"
    INFO = "INFO"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @classmethod
    def coerce(cls, value) -> "Severity":
        """A member from itself or its name (case-insensitive)."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.upper())
            except ValueError:
                pass
        names = ", ".join(s.value for s in SEVERITIES)
        raise ValueError(f"not a severity: {value!r} (expected one of {names})")


#: Report order, most severe first; ``Severity.rank`` is the index here.
SEVERITIES: Tuple[Severity, ...] = (Severity.ERROR, Severity.WARN, Severity.INFO)
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}


class Form(Enum):
    """The two forms a plexus can be in (D20): the ONLY two answers of form
    detection. The engine takes this as a parameter and never detects it."""

    SCHEMA = "schema"
    LEGACY = "legacy"

    @classmethod
    def coerce(cls, value) -> "Form":
        """A member from itself, from either form name as a string
        (case-insensitive), or from a sibling enum that IS this vocabulary —
        a class also named ``Form`` whose members are exactly these two
        values, which is how form detection's own enum interoperates.

        Any OTHER enum is refused even when its value happens to spell one of
        ours: ``Tier.LEGACY`` is the tier of a surface, not the form of a
        tree, and ``Form.coerce(Tier.LEGACY)`` silently answering
        ``Form.LEGACY`` would put the whole report on the wrong severity
        table. Strings stay accepted, so a JSON round-trip needs no enum.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, Enum):
            if not _is_form_vocabulary(type(value)):
                raise ValueError(
                    f"not a form: {value!r} — {type(value).__name__} is a "
                    "different vocabulary; pass a Form, one of its two "
                    "values as a string, or an enum that is this vocabulary."
                )
            value = value.value
        if isinstance(value, str):
            try:
                return cls(value.lower())
            except ValueError:
                pass
        values = ", ".join(f.value for f in cls)
        raise ValueError(f"not a form: {value!r} (expected one of {values})")


def _is_form_vocabulary(enum_cls) -> bool:
    """Whether *enum_cls* is a re-spelling of :class:`Form` — same class name,
    same two values, nothing else. Deliberately structural: form detection
    (a sibling module) defines the same two-valued enum, and neither module
    imports the other."""
    return enum_cls.__name__ == "Form" and {
        member.value for member in enum_cls
    } == {member.value for member in Form}


class Tier(Enum):
    """The SCHEMA.md inventory's strictness-tier vocabulary — the tier of the
    SURFACE a code checks, recorded per code for the ``--list-checks``
    inventory. ``machine``: shape violations ERROR, unknown files WARN;
    ``free-form``: content never checked (a code here can only be about the
    name of a thing); ``opaque``: existence only; ``legacy``: a pre-schema
    name, recognised and read in place; ``not-schema``: the host-bucket
    holding form, informational."""

    MACHINE = "machine"
    FREE_FORM = "free-form"
    OPAQUE = "opaque"
    LEGACY = "legacy"
    NOT_SCHEMA = "not-schema"

    @classmethod
    def coerce(cls, value) -> "Tier":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.lower())
            except ValueError:
                pass
        values = ", ".join(t.value for t in cls)
        raise ValueError(f"not a tier: {value!r} (expected one of {values})")


def _repo_relative(path) -> str:
    """Normalise a finding path to a repo-relative POSIX string (``"."`` for
    the plexus root). Absolute paths are refused: the JSON output is a
    consumed contract and must never leak a machine path. A ``..`` hop is
    refused for the same reason — it addresses something outside the tree
    being validated, so it is not a finding about this plexus."""
    if isinstance(path, PurePath):
        text = path.as_posix()
    elif isinstance(path, str):
        text = path
    else:
        raise TypeError(f"finding path must be a str or PurePath, got {type(path).__name__}")
    posix = PurePosixPath(text)
    if posix.is_absolute() or text.startswith("/"):
        raise ValueError(f"finding path must be repo-relative, got {text!r}")
    if ".." in posix.parts:
        raise ValueError(
            f"finding path must stay inside the plexus, got {text!r} "
            "(a `..` hop leaves the tree being validated)"
        )
    return posix.as_posix()


@dataclass(frozen=True)
class Finding:
    """One thing the validator has to say about one path.

    ``code`` is the stable machine code (``<namespace>.<name>``), ``path`` is
    repo-relative POSIX (``"."`` for the root), ``message`` is the one-line
    human text, ``detail`` optional supporting text (a symlink target, the
    offending value). Immutable and compared by value.
    """

    code: str
    severity: Severity
    path: str
    message: str
    detail: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.code, str) or not self.code:
            raise ValueError(f"finding code must be a non-empty str, got {self.code!r}")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError(f"finding message must be a non-empty str, got {self.message!r}")
        if self.detail is not None and not isinstance(self.detail, str):
            raise TypeError(f"finding detail must be a str or None, got {type(self.detail).__name__}")
        object.__setattr__(self, "severity", Severity.coerce(self.severity))
        object.__setattr__(self, "path", _repo_relative(self.path))

    @property
    def sort_key(self) -> Tuple[int, str, str, str, str]:
        """Deterministic report order: severity, then path, then code; the
        message and detail only break ties so the order is total."""
        return (self.severity.rank, self.path, self.code, self.message, self.detail or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "path": self.path,
            "message": self.message,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, doc: Dict[str, Any]) -> "Finding":
        return cls(
            code=doc["code"],
            severity=doc["severity"],
            path=doc["path"],
            message=doc["message"],
            detail=doc.get("detail"),
        )


#: ``<namespace>.<kebab-name>``: lower-case, one dot, no underscores.
CODE_RE = re.compile(r"^(?P<namespace>[a-z][a-z0-9]*)\.(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)$")

#: The check modules, by the namespace each owns (module docstring). Sorted.
NAMESPACES: Tuple[str, ...] = (
    "agents",
    "index",
    "manifest",
    "record",
    "registry",
    "runner",
    "tree",
)


@dataclass(frozen=True)
class CheckCode:
    """One registered code: its schema-form ``severity``, its ``legacy``-tree
    severity (identical unless D14 downgrades it), the inventory ``tier`` of
    the surface it checks, and a one-line ``summary`` for ``--list-checks``."""

    code: str
    severity: Severity
    legacy: Severity
    tier: Tier
    summary: str

    @property
    def namespace(self) -> str:
        return self.code.split(".", 1)[0]

    @property
    def tier_sensitive(self) -> bool:
        """True when a legacy tree softens this code (D14)."""
        return self.legacy is not self.severity

    def severity_for(self, form) -> Severity:
        return self.legacy if Form.coerce(form) is Form.LEGACY else self.severity

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "legacy": self.legacy.value,
            "tier": self.tier.value,
            "summary": self.summary,
        }


class CheckRegistry:
    """The code table: one entry per code, registered once, enumerable in
    code order. :data:`CHECKS` is the process-wide instance; tests build
    their own."""

    def __init__(self):
        self._codes: Dict[str, CheckCode] = {}

    def register(self, code, severity, *, tier, summary, legacy=None) -> CheckCode:
        """Register *code* with its schema-form *severity*. *legacy* is the
        legacy-tree severity and is given ONLY for tier-sensitive codes; it
        must be a strict downgrade. Duplicate, malformed and out-of-namespace
        codes raise ``ValueError``."""
        if not isinstance(code, str) or not CODE_RE.match(code):
            raise ValueError(
                f"malformed check code {code!r}: expected <namespace>.<kebab-name> "
                f"matching {CODE_RE.pattern}"
            )
        namespace = code.split(".", 1)[0]
        if namespace not in NAMESPACES:
            raise ValueError(
                f"check code {code!r} uses unknown namespace {namespace!r}; "
                f"known: {', '.join(NAMESPACES)}"
            )
        if code in self._codes:
            raise ValueError(f"check code {code!r} is already registered")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"check code {code!r} needs a non-empty summary")
        default = Severity.coerce(severity)
        downgraded = default if legacy is None else Severity.coerce(legacy)
        if legacy is not None and downgraded.rank <= default.rank:
            raise ValueError(
                f"check code {code!r}: legacy severity {downgraded.value} is not a "
                f"downgrade of {default.value} (D14 only ever softens)"
            )
        entry = CheckCode(
            code=code,
            severity=default,
            legacy=downgraded,
            tier=Tier.coerce(tier),
            summary=summary.strip(),
        )
        self._codes[code] = entry
        return entry

    def __contains__(self, code) -> bool:
        return code in self._codes

    def __getitem__(self, code) -> CheckCode:
        try:
            return self._codes[code]
        except KeyError:
            raise KeyError(f"unregistered check code {code!r}") from None

    def get(self, code) -> Optional[CheckCode]:
        return self._codes.get(code)

    def __iter__(self) -> Iterator[CheckCode]:
        return iter(sorted(self._codes.values(), key=lambda entry: entry.code))

    def __len__(self) -> int:
        return len(self._codes)

    def severity_for(self, code, form) -> Severity:
        """The severity *code* carries in a tree of *form*."""
        return self[code].severity_for(form)

    def tier_sensitive(self) -> Tuple[CheckCode, ...]:
        """The D14-downgraded codes, in code order."""
        return tuple(entry for entry in self if entry.tier_sensitive)

    def inventory(self) -> List[Dict[str, str]]:
        """The ``--list-checks`` document: one row per code, in code order."""
        return [entry.to_dict() for entry in self]


#: The process-wide code table every check module registers against.
CHECKS = CheckRegistry()

#: ``register(code, severity, *, tier, summary, legacy=None)`` on :data:`CHECKS`.
register = CHECKS.register


class Report:
    """The collector one validation run fills.

    Holds the tree's :class:`Form`, the findings, and the "unusable" state.
    ``findings`` is always in deterministic order (:attr:`Finding.sort_key`)
    whatever the insertion order, so the text and JSON outputs are stable
    across runs and check ordering.
    """

    def __init__(self, form, *, checks: Optional[CheckRegistry] = None):
        self.form = Form.coerce(form)
        self.checks = CHECKS if checks is None else checks
        self._findings: List[Finding] = []
        self._unusable: Optional[str] = None

    # -- collection ---------------------------------------------------------

    def add(self, code, path, message, *, detail=None, form=None) -> Finding:
        """Emit *code* at *path*, severity resolved from the registry for the
        report's form (or *form*, the host-bucket override). ``KeyError`` for
        a code no module registered."""
        entry = self.checks[code]
        severity = entry.severity_for(self.form if form is None else form)
        finding = Finding(code=code, severity=severity, path=path, message=message, detail=detail)
        self._findings.append(finding)
        return finding

    def append(self, finding: Finding) -> Finding:
        """Add a pre-built finding as is (no severity resolution)."""
        if not isinstance(finding, Finding):
            raise TypeError(f"expected a Finding, got {type(finding).__name__}")
        self._findings.append(finding)
        return finding

    def extend(self, findings: Iterable[Finding]) -> None:
        for finding in findings:
            self.append(finding)

    def mark_unusable(self, reason: str) -> None:
        """Record that the input could not be validated at all (exit 2)."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("an unusable report needs a non-empty reason")
        self._unusable = reason.strip()

    # -- views --------------------------------------------------------------

    @property
    def unusable(self) -> Optional[str]:
        return self._unusable

    @property
    def findings(self) -> Tuple[Finding, ...]:
        return tuple(sorted(self._findings, key=lambda finding: finding.sort_key))

    def by_severity(self, severity) -> Tuple[Finding, ...]:
        wanted = Severity.coerce(severity)
        return tuple(finding for finding in self.findings if finding.severity is wanted)

    @property
    def errors(self) -> Tuple[Finding, ...]:
        return self.by_severity(Severity.ERROR)

    @property
    def warnings(self) -> Tuple[Finding, ...]:
        return self.by_severity(Severity.WARN)

    @property
    def infos(self) -> Tuple[Finding, ...]:
        return self.by_severity(Severity.INFO)

    @property
    def counts(self) -> Dict[str, int]:
        counts = {severity.value.lower(): 0 for severity in SEVERITIES}
        for finding in self._findings:
            counts[finding.severity.value.lower()] += 1
        return counts

    @property
    def has_errors(self) -> bool:
        return any(finding.severity is Severity.ERROR for finding in self._findings)

    @property
    def exit_code(self) -> int:
        if self._unusable is not None:
            return EXIT_UNUSABLE
        if self.has_errors:
            return EXIT_ERRORS
        return EXIT_CLEAN

    def __len__(self) -> int:
        return len(self._findings)

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """The JSON-serialisable document: plain types only, findings in
        report order. The CLI task adds its own envelope around this."""
        return {
            "form": self.form.value,
            "exit_code": self.exit_code,
            "unusable": self._unusable,
            "counts": self.counts,
            "findings": [finding.to_dict() for finding in self.findings],
        }

    @classmethod
    def from_dict(cls, doc: Dict[str, Any], *, checks: Optional[CheckRegistry] = None) -> "Report":
        """Rebuild a report from :meth:`to_dict` output. Findings are taken
        as written (no re-resolution); a document whose ``exit_code`` does
        not match its content is rejected."""
        report = cls(doc["form"], checks=checks)
        report.extend(Finding.from_dict(item) for item in doc.get("findings", ()))
        if doc.get("unusable") is not None:
            report.mark_unusable(doc["unusable"])
        if "exit_code" in doc and doc["exit_code"] != report.exit_code:
            raise ValueError(
                f"report document claims exit_code {doc['exit_code']!r} but its "
                f"content yields {report.exit_code}"
            )
        return report


# ---------------------------------------------------------------------------
# The D14 tier table
# ---------------------------------------------------------------------------
# Integrity findings a legacy tree softens (D14, SCHEMA.md "Legacy tier"),
# and the shape findings the same sentences keep at ERROR in every form.
# The emitting modules (agents, record, index) use these codes.

register(
    "index.dangling-link",
    Severity.ERROR,
    legacy=Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a `_specs/`/`_plans/` index symlink or a `<session-dir>/<artifact>.md` "
        "bundle link whose target does not exist"
    ),
)
register(
    "index.link-not-relative",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "an index or bundle-artifact symlink with an absolute target "
        "(a shape violation in every form)"
    ),
)
register(
    "index.link-escapes",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "an index or bundle-artifact symlink resolving outside the plexus root "
        "(every form)"
    ),
)
register(
    "index.entry-without-file",
    Severity.ERROR,
    legacy=Severity.WARN,
    tier=Tier.MACHINE,
    summary="a `PLANS.md` entry whose linked plan file does not exist",
)
register(
    "agents.memory-entry-without-file",
    Severity.ERROR,
    legacy=Severity.WARN,
    tier=Tier.MACHINE,
    summary="a `MEMORY.md` entry whose linked memory file does not exist",
)
register(
    "agents.memory-scope-mismatch",
    Severity.WARN,
    legacy=Severity.INFO,
    tier=Tier.MACHINE,
    summary=(
        "a memory whose `metadata.type` does not select the scope it lives in "
        "(D17; recall unions both scopes, so nothing is lost)"
    ),
)
register(
    "record.state-unparseable",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "a state file (state.yaml, ledger.yaml, plan.index.json) that does not "
        "parse — broken in every form"
    ),
)
register(
    "record.state-schema",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="a state file whose `schema:` is not an integer (a bool is not one)",
)
register(
    "record.state-enum",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="a state file enum (run status, task status) outside its RUN-STATE.md range",
)
register(
    "record.events-line-unparseable",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="an `events.jsonl` line that is not one JSON object",
)
