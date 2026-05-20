"""
``lmer-pipe`` — a thin client for the supervisor's FastAPI control plane.

Usage:

    lmer-pipe send "/start"           # write to claude's stdin and submit (CR appended)
    lmer-pipe read                    # print everything claude has produced
    lmer-pipe follow                  # stream output until interrupted
    lmer-pipe health                  # check that the endpoint is alive

Connection settings are picked up from environment variables when present:

    LMER_FASTAPI_PORT   — port number (no default)
    LMER_FASTAPI_TOKEN  — bearer token (no default)
    LMER_FASTAPI_HOST   — hostname (default 127.0.0.1)
    LMER_FASTAPI_URL    — full base URL, overrides host+port

Any of these can be overridden with the corresponding ``--port``, ``--token``,
``--host``, ``--url`` flags. The host CLI exports ``LMER_FASTAPI_PORT`` /
``LMER_FASTAPI_TOKEN`` into the in-container environment, so a process spawned
by Claude can use ``lmer-pipe`` with no arguments. From the host, set both
once after launch and every subcommand becomes a one-liner.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

import requests


DEFAULT_HOST = "127.0.0.1"


class PipeError(RuntimeError):
    """Raised when settings are missing or the endpoint cannot be reached."""


def _resolve_base_url(args: argparse.Namespace) -> str:
    """Build the base URL from flags falling back to env vars."""
    url = args.url or os.environ.get("LMER_FASTAPI_URL")
    if url:
        return url.rstrip("/")
    host = args.host or os.environ.get("LMER_FASTAPI_HOST") or DEFAULT_HOST
    port = args.port or os.environ.get("LMER_FASTAPI_PORT")
    if not port:
        raise PipeError(
            "no port configured. Pass --port, --url, or set "
            "LMER_FASTAPI_PORT (printed by lmer at startup)."
        )
    return f"http://{host}:{port}"


def _resolve_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("LMER_FASTAPI_TOKEN")
    if not token:
        raise PipeError(
            "no token configured. Pass --token or set LMER_FASTAPI_TOKEN "
            "(printed by lmer at startup)."
        )
    return token


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def cmd_send(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    token = _resolve_token(args)
    payload = {"data": args.text, "append_newline": not args.no_newline}
    resp = requests.post(
        f"{base}/input",
        json=payload,
        headers={**_auth_headers(token), "Content-Type": "application/json"},
        timeout=args.timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if args.json:
        print(json.dumps(body))
    elif not args.quiet:
        print(f"sent {body.get('bytes_written', 0)} bytes", file=sys.stderr)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    token = _resolve_token(args)
    params = {"cursor": args.since, "timeout": args.wait}
    # Client HTTP timeout must outlast the server-side long-poll budget,
    # otherwise --wait > --timeout would raise ReadTimeout while the server
    # is still legitimately blocking. Mirror cmd_follow's slack formula.
    http_timeout = args.timeout + max(0.0, args.wait) + 5.0 if args.wait > 0 else args.timeout
    resp = requests.get(
        f"{base}/output",
        params=params,
        headers=_auth_headers(token),
        timeout=http_timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if args.json:
        print(json.dumps(body))
        return 0
    if body.get("dropped_bytes"):
        print(
            f"# warning: {body['dropped_bytes']} bytes evicted from "
            f"buffer before this read",
            file=sys.stderr,
        )
    sys.stdout.write(body.get("data", ""))
    sys.stdout.flush()
    if args.print_cursor:
        print(f"\n# cursor={body.get('cursor', 0)}", file=sys.stderr)
    return 0


def cmd_follow(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    token = _resolve_token(args)

    cursor = args.since
    if args.from_end:
        # Probe healthz to learn the current end-of-buffer offset, then start there.
        probe = requests.get(
            f"{base}/healthz", headers=_auth_headers(token), timeout=args.timeout,
        )
        probe.raise_for_status()
        cursor = probe.json().get("cursor", 0)

    long_poll = max(0.0, min(args.wait, 30.0))
    headers = _auth_headers(token)
    while True:
        try:
            resp = requests.get(
                f"{base}/output",
                params={"cursor": cursor, "timeout": long_poll},
                headers=headers,
                timeout=args.timeout + long_poll + 5.0,
            )
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as exc:
            # Genuine transport hiccups: retry. HTTPError (401/403/404/5xx)
            # falls through to main()'s handler so the user sees a real exit
            # code instead of the loop silently retrying forever.
            print(f"# follow: connection error: {exc}", file=sys.stderr)
            time.sleep(args.retry)
            continue
        body = resp.json()
        if body.get("dropped_bytes"):
            print(
                f"\n# warning: {body['dropped_bytes']} bytes evicted from "
                f"buffer before this read",
                file=sys.stderr,
            )
        chunk = body.get("data", "")
        if chunk:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        cursor = body.get("cursor", cursor)


def cmd_health(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    token = _resolve_token(args)
    resp = requests.get(
        f"{base}/healthz", headers=_auth_headers(token), timeout=args.timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if args.json:
        print(json.dumps(body))
    else:
        ok = body.get("ok")
        cursor = body.get("cursor")
        print(f"ok={ok} cursor={cursor}")
    return 0


def _add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", help="Endpoint host (default: $LMER_FASTAPI_HOST or 127.0.0.1)")
    p.add_argument("--port", help="Endpoint port (default: $LMER_FASTAPI_PORT)")
    p.add_argument("--url", help="Full base URL; overrides --host and --port")
    p.add_argument("--token", help="Bearer token (default: $LMER_FASTAPI_TOKEN)")
    p.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds (default 10)")
    p.add_argument("--json", action="store_true", help="Print the raw JSON response instead of human output")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lmer-pipe",
        description="Talk to a running lmer session via its FastAPI endpoint.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    send = sub.add_parser("send", help="Write text to claude's stdin and submit (CR appended unless --no-newline)")
    send.add_argument("text", help="String to write")
    send.add_argument("--no-newline", action="store_true", help="Do not append a trailing CR (the equivalent of Enter); the text is delivered raw")
    send.add_argument("--quiet", action="store_true", help="Suppress the 'sent N bytes' confirmation")
    _add_common_flags(send)
    send.set_defaults(func=cmd_send)

    read = sub.add_parser("read", help="Print buffered output and exit")
    read.add_argument("--since", type=int, default=0, help="Cumulative byte offset to start at (default 0 = beginning)")
    read.add_argument("--wait", type=float, default=0.0, help="Long-poll up to N seconds for new data (default 0 = no wait)")
    read.add_argument("--print-cursor", action="store_true", help="Print the next cursor to stderr after the data")
    _add_common_flags(read)
    read.set_defaults(func=cmd_read)

    follow = sub.add_parser("follow", help="Stream output continuously, like tail -f")
    follow.add_argument("--since", type=int, default=0, help="Cumulative byte offset to start at (default 0 = beginning)")
    follow.add_argument("--from-end", action="store_true", help="Skip backlog; start at the current end of the buffer")
    follow.add_argument("--wait", type=float, default=15.0, help="Long-poll seconds per request (default 15, max 30)")
    follow.add_argument("--retry", type=float, default=2.0, help="Seconds to wait before retrying on connection error")
    _add_common_flags(follow)
    follow.set_defaults(func=cmd_follow)

    health = sub.add_parser("health", help="Probe the endpoint via /healthz")
    _add_common_flags(health)
    health.set_defaults(func=cmd_health)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PipeError as exc:
        print(f"lmer-pipe: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except requests.HTTPError as exc:
        resp = exc.response
        body_preview = ""
        if resp is not None:
            try:
                body_preview = resp.text[:200]
            except Exception:
                body_preview = ""
        status = resp.status_code if resp is not None else "?"
        print(f"lmer-pipe: HTTP {status}: {body_preview}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"lmer-pipe: connection error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
