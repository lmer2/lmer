"""Atomic child-record dumps for the platform test stubs (issue #468).

The platform tests spawn a stub that records what it received — its
environment, or a JSON payload — and then poll for that file. A plain write is
not safe to poll: the bytes reach disk through buffered stdio, so a poll can
land on a prefix of the record, and the variables these tests assert on are the
ones ``spawn_session`` appends at the tail, exactly what a truncated read
loses. Publish by rename instead: a rename is atomic, so the dump path is
either absent or whole, and the existing ``wait_for(is_file and size)`` poll
becomes correct with no sentinel.
"""

from pathlib import Path


def env_dump_shell(dump: Path) -> str:
    """A shell statement publishing the environment to *dump* atomically."""
    return f'env > "{dump}.partial.$$" && mv -f "{dump}.partial.$$" "{dump}"'


def json_dump_python(dump: Path, source: str = "payload") -> str:
    """Python source publishing ``json.dumps(<source>)`` to *dump* atomically.

    The generated stub must have ``json`` and ``os`` in scope.
    """
    tmp = f"{dump}.partial"
    return (
        f"with open({str(tmp)!r}, 'w', encoding='utf-8') as _fh:\n"
        f"    _fh.write(json.dumps({source}))\n"
        f"os.replace({str(tmp)!r}, {str(dump)!r})\n"
    )
