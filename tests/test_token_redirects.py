"""No GitLab PRIVATE-TOKEN follows a redirect to another origin (#593).

Both GitLab clients are driven against two local servers on 127.0.0.1: the
first answers 302 to the second (a different port, so a different origin to
both urllib and requests), and the test reads what headers each server saw.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gitlab_reviewer.client import GitLabClient
from lmer_cli.gitlab_pipeline import GitLabPipelineClient
from tests.timing_budgets import JOIN

FAKE_TOKEN = "glpat-FAKE-redirect-0000000000"


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """Keep both clients talking to 127.0.0.1, not a proxy from the env."""
    for name in (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")


@contextmanager
def _server(redirect_to=None):
    """A server recording each request's path and token; GET /hop redirects."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("PRIVATE-TOKEN")))
            if self.path.endswith("/hop") and redirect_to is not None:
                self.send_response(302)
                self.send_header("Location", redirect_to())
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=JOIN)
        assert not thread.is_alive()


def _base(server):
    return f"http://127.0.0.1:{server.server_port}/api/v4"


def _reviewer_get(base_url):
    client = GitLabClient("gitlab.example.test", FAKE_TOKEN)
    client.base_url = base_url
    return client._request("GET", "hop")


def _pipeline_get(base_url):
    client = GitLabPipelineClient("gitlab.example.test", FAKE_TOKEN)
    client.base_url = base_url
    status, _body = client.request("/hop")
    return status


@pytest.mark.parametrize(
    "fetch", [_reviewer_get, _pipeline_get], ids=["gitlab_reviewer", "gitlab_pipeline"]
)
def test_token_does_not_follow_a_redirect_to_another_origin(fetch):
    with (
        _server() as (target, target_seen),
        _server(lambda: f"{_base(target)}/landed") as (origin, origin_seen),
    ):
        fetch(_base(origin))

    assert origin_seen == [("/api/v4/hop", FAKE_TOKEN)]
    # The redirect was followed — the leak check below is not vacuous.
    assert target_seen == [("/api/v4/landed", None)]


def test_reviewer_token_follows_a_same_origin_redirect():
    """Only a cross-origin hop drops the token; GitLab's own redirects work."""
    holder = {}
    with _server(lambda: f"{_base(holder['server'])}/landed") as (server, seen):
        holder["server"] = server
        assert _reviewer_get(_base(server)) == {"ok": True}

    assert seen == [("/api/v4/hop", FAKE_TOKEN), ("/api/v4/landed", FAKE_TOKEN)]


def test_pipeline_token_is_sent_on_the_first_hop_only():
    """urllib cannot scope a header by origin, so the token is never
    redirected — a same-origin redirect lands unauthenticated."""
    holder = {}
    with _server(lambda: f"{_base(holder['server'])}/landed") as (server, seen):
        holder["server"] = server
        assert _pipeline_get(_base(server)) == 200

    assert seen == [("/api/v4/hop", FAKE_TOKEN), ("/api/v4/landed", None)]
