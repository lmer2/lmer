"""The two halves of the upload mount that live outside ``lmer_platform`` (#246).

``LMER_UPLOADS_DIR`` is set by the platform on the host process and read *inside*
the container — by the prompt fragment that tells an agent where attachments
land. Between those two ends sits ``lmer``'s container environment dict, which is
an allowlist: a variable missing from it is set on the host and simply never
arrives, with nothing failing on the way. That is the convention's own reason for
requiring a source-level guard (``work read-project-info``, env-vars.md §4), and
it is what this module is.
"""

import re
from pathlib import Path

from lmer_platform import uploads

CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


def test_cli_env_dict_declares_the_upload_dir():
    """Guard: LMER_UPLOADS_DIR must be in cli.py's container env dict.

    Without the entry the platform's mount still happens and the variable is set
    on the host `lmer` process, so nothing looks broken — but no agent is ever
    told the store is there, because the fragment inside the container is gated
    on a variable that never crossed.
    """
    pattern = re.compile(
        r"""["']LMER_UPLOADS_DIR["']\s*:\s*os\.environ\.get\(\s*"""
        r"""["']LMER_UPLOADS_DIR["']\s*\)"""
    )
    assert pattern.search(CLI_PY.read_text(encoding="utf-8")), (
        "LMER_UPLOADS_DIR entry missing from cli.py's container env dict"
    )


def test_the_variable_name_is_the_one_the_platform_sets():
    """The literal above and the constant the spawn writes are one fact."""
    assert uploads.UPLOADS_DIR_ENV == "LMER_UPLOADS_DIR"


def test_the_variable_carries_a_container_path():
    """The host path exists only on the daemon's side of the mount, so handing
    it down would name a directory that is not there — the same mistake the ask
    channel's own test guards against."""
    assert uploads.CONTAINER_UPLOADS_DIR.startswith("/home/developer/")
