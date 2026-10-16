"""Schema-2 spawn-harness exec boundary and post-exit lifecycle helper.

The trampoline becomes the harness with exec, so its PID and start metadata
describe the actual child process. Record creation precedes any child work
command; the supervising wrapper records sign-out after reaping that process.
"""

from contextlib import redirect_stdout
import json
import os
import socket
import sys
from datetime import datetime, timezone

from work_repo import cli, session_record


def main(argv: list[str] | None = None) -> int:
    """Start and exec an independent child, or finish its reaped record."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["end"]:
        return cli.cmd_session_end()
    if not args or args[0] != "exec" or len(args) < 2:
        raise ValueError("expected exec <harness command> or end")
    metadata = session_record.provided_metadata()
    metadata.update(pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat(),
                    execution_hostname=socket.gethostname())
    os.environ[session_record.METADATA_ENV] = json.dumps(metadata)
    # Lifecycle diagnostics must not become the harness result: the supervisor
    # uses stdout to distinguish an empty response from usable agent output.
    with redirect_stdout(sys.stderr):
        rc = cli.cmd_session_start()
    if rc:
        return rc
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(args[1], args[1:], os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
