#!/Agents/global/.venv/bin/python3
"""Render a Jinja2 prompt-fragment template with LMER_* env vars to stdout.

Used by ``claude-runner.sh`` to inject prompt fragments (e.g. human identity)
into the AGENTS.md system prompt without hard-coding their text in shell.

Usage: ``render-prompt-fragment.py <template-path>``

The template is loaded from its parent directory (so ``{% include %}`` works
relative to peers), and the rendering context is every ``LMER_*`` environment
variable currently set, minus entries whose name matches a sensitive-key
pattern (TOKEN/KEY/SECRET/PASSWORD/CREDENTIALS) or whose value is a URL with
embedded credentials (e.g. ``https://oauth2:TOKEN@host/...`` in
``LMER_REPO_URL`` / ``LMER_WORK_REPO``). This keeps tokens out of the
rendered system prompt even if a template author references them by name.

The taskdef renderer (``hooks/start.py``) applies the same name rule but
deliberately *redacts* credentialed URL values in place instead of dropping
them — taskdefs legitimately print the repo URL; fragments never need it, so
the simpler drop rule stays here. This renderer also stays self-contained
(no lmer imports): it must run from any install location the runner scripts
find it at, with only jinja2 available.

The rendered output is written to stdout. Any error (missing file, render
failure) is reported on stderr with a non-zero exit status so the caller can
decide how to handle it.
"""
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, TemplateError

_SENSITIVE_NAME_RE = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIALS", re.IGNORECASE)


def _is_sensitive(name: str, value: str) -> bool:
    """True if an env var is unsafe to expose to a prompt-fragment template."""
    if _SENSITIVE_NAME_RE.search(name):
        return True
    if "://" in value and "@" in value:
        try:
            parsed = urlparse(value)
        except ValueError:
            return False
        if parsed.username or parsed.password:
            return True
    return False


def _build_context() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k.startswith("LMER_") and not _is_sensitive(k, v)
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: render-prompt-fragment.py <template-path>", file=sys.stderr)
        return 2

    template_path = Path(argv[1])
    if not template_path.is_file():
        print(f"render-prompt-fragment: template not found: {template_path}", file=sys.stderr)
        return 1

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        keep_trailing_newline=True,
    )
    try:
        template = env.get_template(template_path.name)
        sys.stdout.write(template.render(**_build_context()))
    except TemplateError as exc:
        print(f"render-prompt-fragment: render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
