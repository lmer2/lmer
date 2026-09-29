"""Exec an assistant only after its parent publishes durable ownership/state."""
import os
import select
import sys


def main():
    descriptor, error_descriptor = map(int, sys.argv[1:3])
    os.set_inheritable(error_descriptor, False)
    ready, _, _ = select.select([descriptor], [], [], 60)
    authorized = bool(ready) and os.read(descriptor, 1) == b"1"
    os.close(descriptor)
    if not authorized:
        return 1
    try:
        os.execvp(sys.argv[3], sys.argv[3:])
    except OSError as exc:
        os.write(error_descriptor, str(exc).encode()[:4096])
        return 1


if __name__ == "__main__":
    sys.exit(main())
