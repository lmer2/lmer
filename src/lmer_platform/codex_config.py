"""Run-owned Codex homes. Scope and upgrade contract: docs/CODEX-CONFIG.md."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile

from . import registry, store
from .project_context import assistant_dir

logger = logging.getLogger(__name__)

CONTAINER_DIR = '/home/developer/.codex'
SEED_ENV = 'LMER_CODEX_CONFIG_SEED'


def prepare(
    host: str | None, project: str | None, slug: str, kind: str, session_id: str,
    *, codex: bool = True,
) -> tuple[Path, str]:
    """Prepare a durable scope; an existing config always wins over the seed.

    Returns the directory and, when the seed could not be read, why (else "").
    An unreadable seed raises here only for a launch already known to be codex,
    naming the setting and the path. Any other launch gets one warning and the
    reason back: the child, which alone knows its final harness, refuses a codex
    launch on it (:data:`lmer_cli.harness.CODEX_SEED_ERROR_ENV`).
    """
    identity = json.dumps([host, project, slug], ensure_ascii=True).encode()
    if kind == registry.ASSISTANT_KIND:
        scope = 'assistant'
    elif host and project and slug:
        scope = hashlib.sha256(identity).hexdigest()
    else:
        scope = 'untracked-' + hashlib.sha256(session_id.encode()).hexdigest()
    directory = store.platform_dir() / 'codex-config' / scope
    if kind == registry.ASSISTANT_KIND:
        directory = assistant_dir() / 'codex-config' / scope
    store.ensure_state_dir(directory)
    target = directory / 'config.toml'
    seed = os.environ.get(SEED_ENV, '').strip()
    if seed and not target.exists():
        source = Path(seed).expanduser()
        try:
            input_file = source.open('rb')
        except OSError as exc:
            reason = f'{SEED_ENV} names {source}, which cannot be read ({exc.strerror or exc})'
            if codex:
                raise OSError(exc.errno, reason) from exc
            logger.warning(
                'codex_config_seed_skipped %s; the session refuses it if it resolves codex', reason,
            )
            return directory, reason
        with input_file:
            fd, name = tempfile.mkstemp(dir=directory, prefix='.seed-')
            try:
                with os.fdopen(fd, 'wb') as output:
                    shutil.copyfileobj(input_file, output)
                try:
                    os.link(name, target)
                except FileExistsError:
                    pass
            finally:
                os.unlink(name)
    return directory, ''


def mount_flags(
    host: str | None, project: str | None, slug: str, kind: str, session_id: str,
    *, codex: bool = True,
) -> tuple[list[str], str]:
    """The directory mount preserves atomic config-file replacements.

    Also returns :func:`prepare`'s unreadable-seed reason, for the child.
    """
    directory, seed_error = prepare(host, project, slug, kind, session_id, codex=codex)
    return ['--mount-dir', f'{directory}:{CONTAINER_DIR}:rw'], seed_error
