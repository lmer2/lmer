"""One optional Slack request in a process its caller can kill and reap."""
import json
import sys

import requests


def main() -> None:
    request = json.load(sys.stdin)
    try:
        with requests.Session() as session:
            session.headers.update(request["headers"])
            with session.post(request["url"], json=request["payload"],
                              timeout=request["timeout"], stream=True) as response:
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Expected a Slack response object")
                result = {"data": data}
    except requests.HTTPError as exc:
        result = {"failure": f"HTTP {exc.response.status_code}"}
    except (requests.RequestException, ValueError) as exc:
        result = {"failure": type(exc).__name__}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
