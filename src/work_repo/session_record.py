"""Opaque execution identity and extensible, credential-safe launch metadata.

A session UID names one agent execution. Prior executions are read-only context;
helper commands share the UID and metadata supplied by their launch process.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .utils import is_secret_env_name, redact_secrets, redact_token_shapes, load_work_manifest

UID_RE = re.compile(r"[0-9a-f]{32}")
METADATA_ENV = "LMER_SESSION_METADATA"
SOURCE_ENV = "LMER_SOURCE_SESSION_ID"
SOURCE_ACK_ENV = "LMER_ALLOW_UNSIGNED_SOURCE"


def schema_version(root: Path | None = None) -> int:
    """Read the actual tree version, refusing a present unusable manifest."""
    root = root or Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
    manifest = load_work_manifest(root)
    return manifest.schema if manifest is not None else 0


def scrub_metadata(value):
    """Preserve arbitrary JSON-shaped data while removing credential values.

    Three rules, at every nesting level: a value under a secret-looking key
    (``is_secret_env_name``, ``authorization``, ``cookie``) is redacted whole;
    a string containing a value this process holds in a secret-named env var
    is redacted where it occurs; and a token-shaped substring (known provider
    prefixes, AWS key ids, PEM private keys) is redacted even under a benign
    key and even when this process does not hold it. An unprefixed secret
    under a benign key that this process does not hold is not detectable.
    """
    if isinstance(value, dict):
        return {
            key: "***REDACTED***"
            if is_secret_env_name(str(key)) or str(key).lower() in {"authorization", "cookie"}
            else scrub_metadata(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub_metadata(item) for item in value]
    return redact_token_shapes(redact_secrets(value)) if isinstance(value, str) else value


def provided_metadata() -> dict:
    """Load the scrubbed freeform metadata envelope supplied by the host."""
    raw = os.environ.get(METADATA_ENV, "{}")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{METADATA_ENV} must contain a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{METADATA_ENV} must contain a JSON object")
    return scrub_metadata(value)


def compose_launch_metadata(
    supplied: dict | None,
    *,
    harness: str | None,
    model: str | None,
    effort: str | None,
    config: dict | None,
) -> dict:
    """Merge one host launch's resolved facts into extensible metadata.

    Unknown fields survive. Resolved harness/model/effort/config values replace
    inherited selections, including explicit ``None`` values, so a predecessor's
    launch choices cannot describe a fresh execution. Supplied top-level
    operator/hostname remain the originating host provenance; ``launcher`` records
    the process on this host that composed the final launch environment.
    """
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise ValueError(f"{METADATA_ENV} must contain a JSON object")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("session launch config must be a JSON object")

    metadata = scrub_metadata(supplied)
    resolved_config = scrub_metadata(config)
    inherited_config = metadata.get("config")
    merged_config = (
        dict(inherited_config) if isinstance(inherited_config, dict) else {}
    )
    merged_config.update(resolved_config)

    operator = getpass.getuser()
    hostname = socket.gethostname()
    metadata.setdefault("operator", operator)
    metadata.setdefault("hostname", hostname)
    metadata.update(
        harness=harness,
        model=model,
        effort=effort,
        config=merged_config,
    )

    inherited_launcher = metadata.get("launcher")
    launcher = dict(inherited_launcher) if isinstance(inherited_launcher, dict) else {}
    inherited_launcher_config = launcher.get("config")
    launcher_config = (
        dict(inherited_launcher_config)
        if isinstance(inherited_launcher_config, dict)
        else {}
    )
    launcher_config.update(merged_config)
    launcher.update(
        operator=operator,
        hostname=hostname,
        started_at=datetime.now(timezone.utc).isoformat(),
        pid=os.getpid(),
        harness=harness,
        model=model,
        effort=effort,
        config=launcher_config,
    )
    metadata["launcher"] = launcher
    return scrub_metadata(metadata)


def _launch_fields() -> dict:
    return {
        "operator": getpass.getuser(),
        "hostname": socket.gethostname(),
        "execution_hostname": socket.gethostname(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "harness": os.environ.get("LMER_HARNESS"),
        "model": os.environ.get("LMER_LLM_NAME"),
        "effort": os.environ.get("LMER_REASONING_EFFORT"),
        "config": {},
    }


def start_launch() -> dict:
    """Start a new execution, replacing any inherited UID exactly once."""
    metadata = provided_metadata()
    defaults = _launch_fields()
    for key, value in defaults.items():
        metadata.setdefault(key, value)
    # PID/time describe this execution, never a predecessor's metadata.
    metadata.update(
        uid=uuid.uuid4().hex,
        pid=defaults["pid"],
        started_at=defaults["started_at"],
        execution_hostname=defaults["execution_hostname"],
    )
    metadata.pop("source_session_uid", None)
    metadata.pop("unsigned_source_acknowledged", None)
    os.environ["LMER_SESSION_ID"] = metadata["uid"]
    os.environ[METADATA_ENV] = json.dumps(metadata, ensure_ascii=False)
    return metadata


def current_metadata() -> dict:
    """Read the launch dictionary without reminting identity in a helper."""
    uid = os.environ.get("LMER_SESSION_ID", "")
    if UID_RE.fullmatch(uid) is None:
        raise ValueError("schema 2 writes require this launch's opaque session UID")
    metadata = provided_metadata()
    if metadata.get("uid", uid) != uid:
        raise ValueError("session metadata UID does not match this launch")
    for key, value in _launch_fields().items():
        metadata.setdefault(key, value)
    metadata["uid"] = uid
    return metadata


def seeded_session() -> dict:
    """The `session` of a record THIS launch seeds for a later one (D13).

    A fresh uid, this launch named once in `seeded_by`, and the moment of
    seeding — nothing else. The launch's own metadata (operator, hostname,
    harness, model, pid) describes an execution that never ran in that
    record, so none of it is copied: the record says nothing has run in it
    yet and still traces back to whoever seeded it. Like
    :func:`current_metadata`, refuses without this launch's valid UID — a
    seed without a seeder is not a schema 2 write.
    """
    return {
        "uid": uuid.uuid4().hex,
        "seeded_by": current_metadata()["uid"],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def signed_out(events: list[dict]) -> bool:
    """Only an observed final session_end establishes a clean sign-out."""
    lifecycle = [event.get("type") for event in events if event.get("type") in {"session_start", "session_end"}]
    return bool(lifecycle) and lifecycle[-1] == "session_end"


def is_seeded(state: dict, events: list[dict]) -> bool:
    """A record `work seed` left for a later launch and nobody has run in (D13).

    `session.seeded_by` names the launch that wrote it and no `session_start`
    was ever logged there. `seeded_by` alone is not the fact: a record
    something started in is an ordinary record whose seeding is history, and
    `signed_out` is the question to ask of it. Pure, over the same parsed
    events list `signed_out` takes.
    """
    metadata = state.get("session") if isinstance(state, dict) else None
    seeded_by = metadata.get("seeded_by") if isinstance(metadata, dict) else None
    if not isinstance(seeded_by, str) or not seeded_by:
        return False
    return not any(event.get("type") == "session_start" for event in events)


def is_stopped(state: dict, events: list[dict]) -> bool:
    """Whether nothing can still be running in a record: it signed out, or it
    is seeded and nothing ever started in it. The ONE rule every reader of
    "is this record live?" shares — the continuation source check, migration
    and housekeeping — so they cannot drift apart on what a seed is. A caller
    that cannot read the state or the events has no answer here; treating
    such a record as not stopped is that caller's rule.
    """
    return signed_out(events) or is_seeded(state, events)


def sort_key(path: Path) -> tuple[str, str]:
    """Sort opaque records by their logged start time, not random UID bytes."""
    import yaml

    try:
        state = yaml.safe_load((path / "state.yaml").read_text())
        metadata = state.get("session") or {}
        for stamp in (metadata.get("started_at"), state.get("created")):
            if not isinstance(stamp, str):
                continue
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(), path.name
    except (OSError, TypeError, AttributeError, yaml.YAMLError):
        pass
    try:
        modified = path.stat().st_mtime
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc).isoformat(), path.name
    return datetime.fromtimestamp(modified, timezone.utc).isoformat(), path.name
