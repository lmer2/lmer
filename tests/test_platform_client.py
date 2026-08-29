"""``lmer_platform.client`` — the HTTP client every host-side client shares.

The code here was ``ctl``'s and private (``ctl._request``, ``ctl.Endpoint``).
It is public because a second client exists — ``lmer-matrix-bridge`` speaks to
the same daemon from the same host — and these tests pin what promoting it must
not have changed:

- **The credential is a header and nothing else.** Never a query parameter,
  never formatted into the URL, and never in the text of a transport failure.
  A host-side bridge logs its failures where an operator (and a browser) can
  read them.
- **It is a real client of the real app.** The end-to-end cases drive the
  FastAPI app through the ``transport`` seam, so the daemon's own statuses are
  what a caller is seen to get — including the 401 for a credential the daemon
  does not know.
- **It grew no logic of its own in the move.** No retry, no default, no
  interpretation: what came back is returned, whatever it was.
- **``ctl`` consumes it rather than keeping a copy**, so the two cannot drift.

``tests/test_platform_ctl.py`` still exercises the same seam through
``lmer-ctl``'s argv surface and is deliberately untouched by the promotion.
"""

import dataclasses

import pytest
import requests
from fastapi.testclient import TestClient

from lmer_platform import api, client, ctl, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
BASE_URL = "http://testserver"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def config(platform_root):
    return cfg.load()


@pytest.fixture
def fake_state():
    """A canned fleet view, so no test here needs a work repo."""

    def builder(config, *, force_pull=False):
        return {
            "schema": 1,
            "generated_at": "2026-08-22T12:00:00Z",
            "runs": [],
            "attention": [],
            "totals": {"runs": 0, "live": 0, "attention": 0},
        }

    return builder


@pytest.fixture
def app_transport(config, fake_state):
    """The real app as a transport, wrapped to swallow ``timeout``.

    ``TestClient`` warns about a timeout argument — an in-process ASGI call has
    no socket to time one out on. That the argument is forwarded at all is
    :class:`Recorder`'s claim, below.
    """

    class Local:
        def __init__(self, test_client):
            self.client = test_client

        def request(self, method, url, *, timeout=None, **kwargs):
            return self.client.request(method, url, **kwargs)

    return Local(TestClient(api.create_app(config, SECRET, state_builder=fake_state)))


class Recorder:
    """A transport that records the request and answers a canned reply.

    A real reply cannot show *where the credential was put*; this can.
    """

    def __init__(self, status_code=200, text="{}"):
        self.reply = type("Reply", (), {"status_code": status_code, "text": text})()
        self.calls = []

    def request(self, method, url, *, params=None, json=None, headers=None,
                timeout=None):
        self.calls.append({
            "method": method, "url": url, "params": params, "json": json,
            "headers": headers, "timeout": timeout,
        })
        return self.reply


@pytest.fixture
def endpoint():
    return client.Endpoint(BASE_URL, SECRET)


# --- the endpoint ------------------------------------------------------------

def test_the_endpoint_carries_the_credential_in_one_place(endpoint):
    assert endpoint.url("/api/health") == f"{BASE_URL}/api/health"
    assert SECRET not in endpoint.url("/api/health")
    assert endpoint.headers() == {"Authorization": f"Bearer {SECRET}"}


def test_the_endpoint_is_frozen():
    """A caller that could retarget a shared endpoint could send this daemon's
    credential to another host. The exception type is named rather than caught
    as ``Exception``: frozen-ness replaced by something else that also raises
    would otherwise pass this."""
    endpoint = client.Endpoint(BASE_URL, SECRET)
    with pytest.raises(dataclasses.FrozenInstanceError):
        endpoint.base_url = "http://elsewhere"


# --- what is sent ------------------------------------------------------------

def test_the_credential_is_only_ever_a_header(endpoint):
    """The claim the whole module is shaped by: no query string, no URL."""
    recorder = Recorder()
    call = client.Call("POST", "/api/runs/answer", params={"key": "value"},
                       body={"text": "hello"})
    client.request(endpoint, call, timeout=5.0, transport=recorder)

    sent = recorder.calls[0]
    assert sent["headers"] == {"Authorization": f"Bearer {SECRET}"}
    assert SECRET not in sent["url"]
    assert SECRET not in repr(sent["params"])
    assert SECRET not in repr(sent["json"])


def test_the_call_is_forwarded_whole(endpoint):
    recorder = Recorder()
    call = client.Call("GET", "/api/state", params={"force": "1"})
    client.request(endpoint, call, timeout=12.5, transport=recorder)

    assert recorder.calls == [{
        "method": "GET",
        "url": f"{BASE_URL}/api/state",
        "params": {"force": "1"},
        "json": None,
        "headers": {"Authorization": f"Bearer {SECRET}"},
        "timeout": 12.5,
    }]


def test_a_call_with_no_params_or_body_sends_neither(endpoint):
    """``None`` rather than ``{}``: several routes distinguish an absent field
    from an empty one, and the client is not the place that decides."""
    recorder = Recorder()
    client.request(endpoint, client.Call("GET", "/api/health"), timeout=1.0,
                   transport=recorder)
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["json"] is None


def test_extra_headers_ride_along_but_never_replace_the_credential(endpoint):
    """``X-Lmer-Principal`` is why this parameter exists: one credential is
    shared by every Matrix principal the bridge speaks for, so the daemon's log
    would otherwise record only the bridge. A caller that tried to send its own
    ``Authorization`` loses to the endpoint's."""
    recorder = Recorder()
    client.request(
        endpoint, client.Call("POST", "/api/runs/answer"), timeout=5.0,
        transport=recorder,
        headers={"X-Lmer-Principal": "@alice:matrix.example.net",
                 "Authorization": "Bearer not-this-one"},
    )
    sent = recorder.calls[0]["headers"]
    assert sent["X-Lmer-Principal"] == "@alice:matrix.example.net"
    assert sent["Authorization"] == f"Bearer {SECRET}"


# --- against the real app ----------------------------------------------------

def test_a_route_answers_through_the_transport_seam(app_transport):
    """No network, no daemon: the real FastAPI app behind the same seam the
    platform's own tests use."""
    endpoint = client.Endpoint(BASE_URL, SECRET)
    response = client.request(
        endpoint, client.Call("GET", "/api/health"), timeout=5.0,
        transport=app_transport,
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_the_daemons_own_refusal_is_returned_not_interpreted(app_transport):
    """A wrong credential is a 401 the caller sees, not an exception the client
    invents. Nothing here retries and nothing here re-words."""
    endpoint = client.Endpoint(BASE_URL, "not-the-secret")
    response = client.request(
        endpoint, client.Call("GET", "/api/health"), timeout=5.0,
        transport=app_transport,
    )
    assert response.status_code == 401


def test_a_body_reaches_the_route(app_transport):
    """A POST with a body the daemon rejects on its merits — proof the body
    travelled, and that its refusal came back untouched."""
    endpoint = client.Endpoint(BASE_URL, SECRET)
    response = client.request(
        endpoint,
        client.Call("POST", "/api/runs/answer", body={"host": "", "project": "",
                                                      "slug": "", "text": ""}),
        timeout=5.0,
        transport=app_transport,
    )
    assert response.status_code >= 400
    assert response.status_code < 500


# --- when the platform cannot be reached -------------------------------------

class Unreachable:
    """A transport that fails the way ``requests`` does."""

    def __init__(self, message):
        self.message = message

    def request(self, *args, **kwargs):
        raise requests.ConnectionError(self.message)


def test_a_transport_failure_is_named_without_the_credential(endpoint):
    failing = Unreachable(f"connection refused to {BASE_URL}")
    with pytest.raises(client.TransportError) as excinfo:
        client.request(endpoint, client.Call("GET", "/api/health"), timeout=1.0,
                       transport=failing)

    message = str(excinfo.value)
    assert BASE_URL in message, "an operator needs to know which platform"
    assert SECRET not in message
    assert excinfo.value.error == "unreachable"


def test_a_transport_failure_is_a_platform_error(endpoint):
    """``lmer-ctl``'s single ``except CtlError`` must keep catching it."""
    assert issubclass(client.TransportError, client.PlatformError)
    assert issubclass(client.TransportError, ctl.CtlError)
    assert client.PlatformError.error == "configuration"


def test_an_injected_transports_own_bug_is_not_dressed_as_a_fleet_problem(endpoint):
    """Only ``requests``' exceptions become :class:`TransportError`. A fake that
    raises anything else is a bug in the test, and should look like one."""

    class Broken:
        def request(self, *args, **kwargs):
            raise ValueError("fake is wrong")

    with pytest.raises(ValueError):
        client.request(endpoint, client.Call("GET", "/api/health"), timeout=1.0,
                       transport=Broken())


# --- one implementation, two clients -----------------------------------------

def test_ctl_uses_this_module_rather_than_a_copy():
    """The promotion's whole point: ``lmer-ctl`` and the bridge cannot drift,
    because there is one implementation and ``ctl`` re-exports it."""
    assert ctl.Endpoint is client.Endpoint
    assert ctl.Call is client.Call
    assert ctl.TransportError is client.TransportError
    assert ctl.CtlError is client.PlatformError
    assert ctl._request is client.request


def test_ctl_keeps_the_environment_pair_that_is_its_own():
    """``resolve_endpoint`` reads the two variables the *host writes into a
    container*. A host-side client has the daemon's secret file instead, so the
    function stays in ``ctl`` rather than moving with the transport."""
    assert not hasattr(client, "resolve_endpoint")
    assert callable(ctl.resolve_endpoint)


def test_the_client_module_pulls_in_nothing_but_the_transport():
    """A bridge process starts this to make one call. Importing it must not drag
    argparse surfaces, the spawn stack, or the daemon in behind it."""
    import ast
    from pathlib import Path

    source = Path(client.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import is recorded as what it is rather than skipped:
            # `from .something import x` would otherwise pass this guard while
            # dragging in exactly what it exists to keep out.
            imported.add(
                "." * node.level + (node.module or "").split(".")[0]
                if node.level else node.module.split(".")[0]
            )
    assert imported <= {"__future__", "dataclasses", "typing", "requests"}, imported
