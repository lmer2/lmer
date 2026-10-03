"""Read and use a disposable credential after the real connector cwd handoff."""
import json
import os
from pathlib import Path
import sys

from lmer_cli import assistant_mentions
from lmer_cli.connector_ownership import Lease, ThreadKey
from lmer_platform import config
from lmer_platform.client import PlatformError


def main():
    transport, conversation, root = sys.argv[1:]
    lease = Lease.enter(ThreadKey(transport, conversation, root))
    try:
        resolved = config.load()
        result = {
            "cwd": str(Path.cwd()),
            "config_module": config.__file__,
            "path": str(resolved.secret_path),
            "port": resolved.bind_port,
            "container_url": os.environ[config.ENV_CONTAINER_URL],
            "platform_url": os.environ["LMER_PLATFORM_URL"],
            "supervisor_url": os.environ["LMER_SUPERVISOR_URL"],
        }
        try:
            result["found"] = bool(config.read_secret(resolved))
            if resolved.secret_path.exists():
                info = resolved.secret_path.stat()
                result["identity"] = [info.st_dev, info.st_ino]
        except config.ConfigError as exc:
            result["read_error"] = str(exc)
        try:
            result["forward"] = assistant_mentions.forward(transport, conversation, {"id": "fixture-event"})
        except (config.ConfigError, PlatformError) as exc:
            result["forward_error"] = type(exc).__name__
        print(json.dumps(result))
    finally:
        lease.close(release=False)


if __name__ == "__main__":
    main()
