"""The entrypoint's one-time mise node pin migration.

Container-homes seeded before ``.mise.toml`` pinned node keep ``node = "lts"``
in their persistent ``~/.config/mise/config.toml``: the entrypoint only seeds
that file when it is missing, and a current mise resolves ``lts`` to node 26,
which this image cannot run. The migration block rewrites exactly that line.

The block is extracted from the real ``entrypoint.sh`` and executed under
``set -e`` (the ``tests/test_gpg_agent_forwarding.py`` precedent), so its
promises are tested where they live: only the exact line changes, operators'
own entries survive, a second run changes nothing, and a missing file or line
is a silent no-op.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ENTRYPOINT = REPO_ROOT / "Ctl" / "container" / "entrypoint.sh"

# Markers delimiting the block inside entrypoint.sh; renaming the block fails
# these tests loudly rather than silently passing on stale code.
BLOCK_START = "# ── mise node pin migration ──"
BLOCK_END = "# ── end mise node pin migration ──"

SEEDED = """# Mise configuration for LMER container
[tools]
# Default tools (installed at container build time)
node = "lts"
gh = "latest"
glab = "latest"
claude-powerline = "latest"
actionlint = "1.7.7"
terraform = "1.9"

[settings]
experimental = true
"""


def _extract_block() -> str:
    text = ENTRYPOINT.read_text()
    assert BLOCK_START in text, f"entrypoint marker missing: {BLOCK_START!r}"
    assert BLOCK_END in text, f"entrypoint marker missing: {BLOCK_END!r}"
    start = text.index(BLOCK_START)
    end = text.index(BLOCK_END, start)
    return text[start:end]


def _run_block(home: Path) -> subprocess.CompletedProcess:
    script = "set -e\n" + _extract_block() + '\necho "migration-done"\n'
    proc = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc


def _config(home: Path) -> Path:
    return home / ".config" / "mise" / "config.toml"


def _seed(home: Path, text: str) -> Path:
    config = _config(home)
    config.parent.mkdir(parents=True)
    config.write_text(text)
    return config


def test_the_exact_lts_line_becomes_24_and_nothing_else_changes(tmp_path):
    config = _seed(tmp_path, SEEDED)
    proc = _run_block(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "migration-done" in proc.stdout
    assert config.read_text() == SEEDED.replace('node = "lts"\n', 'node = "24"\n')


def test_a_second_run_changes_nothing(tmp_path):
    config = _seed(tmp_path, SEEDED)
    assert _run_block(tmp_path).returncode == 0
    once = config.read_text()
    proc = _run_block(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert config.read_text() == once


def test_an_operators_own_node_choice_is_left_alone(tmp_path):
    # Anything but the exact seeded line is the operator's: a different
    # version, a trailing comment, other spacing, or "lts" on another tool.
    text = (
        '[tools]\nnode = "lts" # mine\nnode="lts"\n  node = "lts"\n'
        'node = "22"\npython = "lts"\n'
    )
    config = _seed(tmp_path, text)
    proc = _run_block(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert config.read_text() == text


def test_a_missing_config_is_a_silent_no_op(tmp_path):
    proc = _run_block(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "migration-done" in proc.stdout
    assert proc.stderr == ""
    assert not _config(tmp_path).exists()


def test_the_migration_runs_before_mise_is_used(tmp_path):
    # The tool-install step reads this config; the rewrite must land first.
    text = ENTRYPOINT.read_text()
    assert text.index(BLOCK_END) < text.index("mise install --yes")
