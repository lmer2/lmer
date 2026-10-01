"""Explicit schema-1 to schema-2 cutover, preserving historical records.

This operation changes only the version scalar. Ambiguous layouts and active
records must be resolved separately; migration never manufactures historical
UIDs, signs out an execution, or silently relocates its files.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import yaml

from .manifest import MANIFEST_NAME, ManifestError, load_manifest
from .registry import RegistryError, build_registry, resolve_project_dir


def _identity_blockers(root: Path, registry) -> list[str]:
    """Check disk ambiguity even when registry routing masks a stale bucket."""
    blockers = []
    for project in registry.projects.values():
        for remote in (project.repo, *project.remotes):
            if not remote:
                continue
            try:
                resolve_project_dir(root, remote, registry=None, schema=2)
            except RegistryError as exc:
                blockers.append(str(exc))
    return list(dict.fromkeys(blockers))


def _record_blockers(root: Path) -> list[str]:
    from .checks.record import iter_run_areas
    from work_repo import run_state, session_record

    blockers = []
    seen = set()
    for area, _layout in iter_run_areas(root):
        for record in run_state.iter_record_dirs(area):
            relative = record.relative_to(root).as_posix()
            try:
                path = run_state._state_path(record)
                state = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    raise ValueError("state is not a mapping")
                if record.parent == area:
                    blockers.append(f"{relative}: move this flat record into a session group first")
                uid = run_state.session_id_of(record.name)
                identity = (area, uid)
                if identity in seen:
                    blockers.append(f"{relative}: duplicate historical session ID {uid}")
                seen.add(identity)
                claim = state.get("claim")
                if isinstance(claim, dict) and claim.get("session_id"):
                    blockers.append(f"{relative}: release claim must be explicitly cleared")
                events = run_state.read_events(record, last_n=0)
                if state.get("status") == "in-progress" and not session_record.signed_out(events):
                    blockers.append(f"{relative}: no recorded sign-out; resolve the prior session first")
            except yaml.YAMLError:
                # Parser messages include source lines, which can contain
                # credentials in historical records.
                blockers.append(f"{relative}: unreadable record (invalid YAML)")
            except (OSError, ValueError) as exc:
                blockers.append(f"{relative}: unreadable record ({exc})")
    return blockers


def migrate(root: Path, *, apply: bool = False) -> dict:
    """Return a reviewable preview; apply only when all preconditions hold."""
    root = Path(root)
    path = root / MANIFEST_NAME
    manifest = load_manifest(root)
    if manifest is None:
        raise ManifestError(path, "first adopt this legacy tree into schema 1")
    result = {"from": manifest.schema, "to": 2, "moves": [], "diff": "",
              "blockers": [], "applied": False}
    if manifest.schema == 2:
        return result
    if path.is_symlink():
        raise ManifestError(path, "migration requires a regular manifest file")
    original = path.read_text(encoding="utf-8")
    node = yaml.compose(original)
    versions = [value for key, value in node.value if key.value == "schema"]
    if len(versions) != 1:
        raise ManifestError(path, "migration requires one explicit schema key")
    scalar = versions[0]
    if any(
        isinstance(token, yaml.tokens.AnchorToken)
        and scalar.start_mark.index <= token.start_mark.index < scalar.end_mark.index
        for token in yaml.scan(original)
    ):
        result["blockers"] = [
            "schema uses a YAML anchor or alias; make it a standalone integer before migration"
        ]
        return result
    updated = original[:scalar.start_mark.index] + "2" + original[scalar.end_mark.index:]
    result["diff"] = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{MANIFEST_NAME}", tofile=f"b/{MANIFEST_NAME}"
    ))
    # Rebuild under both contracts; schema-2 identity must be unambiguous.
    build_registry(manifest)
    registry = build_registry(replace(manifest, schema=2))
    result["blockers"] = _identity_blockers(root, registry) + _record_blockers(root)
    if not apply or result["blockers"]:
        return result
    # Do not overwrite a manifest changed while preflight was reading records.
    if path.read_text(encoding="utf-8") != original:
        raise ManifestError(path, "manifest changed during migration; preview again")
    descriptor, temporary = tempfile.mkstemp(prefix=".plexus-migration-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(updated)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    result["applied"] = True
    return result
