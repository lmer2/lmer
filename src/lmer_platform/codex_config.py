"""Run-owned Codex homes. Scope and upgrade contract: docs/CODEX-CONFIG.md."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from . import registry, store
from .project_context import assistant_dir

CONTAINER_DIR = '/home/developer/.codex'
SEED_ENV = 'LMER_CODEX_CONFIG_SEED'


def prepare(host: str | None, project: str | None, slug: str, kind: str, session_id: str) -> Path:
    """Prepare a durable scope; an existing config always wins over the seed."""
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
        fd, name = tempfile.mkstemp(dir=directory, prefix='.seed-')
        try:
            with os.fdopen(fd, 'wb') as output, source.open('rb') as input_file:
                shutil.copyfileobj(input_file, output)
            try:
                os.link(name, target)
            except FileExistsError:
                pass
        finally:
            os.unlink(name)
    return directory


def mount_flags(host: str | None, project: str | None, slug: str, kind: str, session_id: str) -> list[str]:
    """The directory mount preserves atomic config-file replacements."""
    directory = prepare(host, project, slug, kind, session_id)
    return ['--mount-dir', f'{directory}:{CONTAINER_DIR}:rw']
