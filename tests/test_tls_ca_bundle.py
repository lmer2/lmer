"""The shared CA-bundle fix, and the callers that must actually apply it.

The operator hit this on a second host: ``lmer platform setup-ui`` died with

    cannot download https://nodejs.org/dist/v24.18.0/... ([SSL:
    CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local
    issuer certificate)

The remote was fine. The standalone CPython that ``uv tool install`` fetches can
be compiled with a default ``SSL_CERT_FILE`` path that does not exist on the host,
so ``ssl.create_default_context()`` finds no trust store at all and every
handshake fails. The same cause had already been fixed once for the Slack
socket-mode connection, in a private helper the Node download knew nothing about.

So the tests here are as much about the *second* occurrence as the first: one
implementation, and each network caller demonstrably reaching it.
"""

import os
import ssl

import certifi
import pytest

from lmer_cli import tls
from lmer_platform import ui_build
from slack_chat import listener


# --- the helper itself -------------------------------------------------------

def test_a_host_with_no_usable_trust_store_gets_certifis(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    tls.ensure_ca_bundle()

    assert os.environ["SSL_CERT_FILE"] == certifi.where()


def test_an_operators_own_bundle_is_never_overwritten(monkeypatch):
    """The corporate-CA case, and the reason this is not just `always set it`.

    Behind a TLS-inspecting proxy the operator's CA is the only bundle that can
    verify anything; replacing it with certifi's would break every connection
    this helper exists to fix.
    """
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/corporate-ca.pem")

    tls.ensure_ca_bundle()

    assert os.environ["SSL_CERT_FILE"] == "/custom/corporate-ca.pem"


def test_calling_it_twice_changes_nothing(monkeypatch):
    """It runs on paths that may already have run it; that has to be free."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    tls.ensure_ca_bundle()
    first = os.environ["SSL_CERT_FILE"]
    tls.ensure_ca_bundle()

    assert os.environ["SSL_CERT_FILE"] == first


def test_a_missing_certifi_leaves_the_interpreters_own_default_alone(monkeypatch):
    """Guessing a path would be worse than surfacing the original error."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def refuse_certifi(name, *args, **kwargs):
        if name == "certifi":
            raise ImportError("no certifi here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", refuse_certifi)

    tls.ensure_ca_bundle()

    assert "SSL_CERT_FILE" not in os.environ


def test_the_bundle_it_points_at_can_actually_verify(monkeypatch):
    """Not just "a path was set" — the file has to load as a trust store.

    A plausible-but-unusable path would reproduce the exact bug this fixes while
    passing every assertion above.
    """
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    tls.ensure_ca_bundle()

    context = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    assert context.cert_store_stats()["x509_ca"] > 0


# --- the callers -------------------------------------------------------------

def test_the_node_download_fixes_the_trust_store_before_it_connects(monkeypatch):
    """The bug the operator hit. Ordering is the whole point.

    OpenSSL reads ``SSL_CERT_FILE`` when a context loads its default certs, so a
    call made *after* the request is opened fixes nothing. This asserts the
    sequence, not merely that both things happen.
    """
    events = []
    monkeypatch.setattr(ui_build, "ensure_ca_bundle", lambda: events.append("ca"))

    def fake_urlopen(*_args, **_kwargs):
        events.append("connect")
        raise OSError("stop here, the ordering is what matters")

    monkeypatch.setattr(ui_build.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ui_build.UIBuildError):
        ui_build._download("https://nodejs.org/dist/x.tar.xz", ui_build.Path("/tmp/x"))

    assert events == ["ca", "connect"], (
        f"the trust store must be fixed before the handshake, got {events}"
    )


def test_a_verify_failure_says_where_to_look(monkeypatch):
    """The bare OpenSSL message reads as "nodejs.org is untrusted", which sends
    the operator to the wrong place — it is the local bundle that is missing."""
    monkeypatch.setattr(ui_build, "ensure_ca_bundle", lambda: None)

    def fake_urlopen(*_args, **_kwargs):
        raise OSError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable "
            "to get local issuer certificate (_ssl.c:1028)"
        )

    monkeypatch.setattr(ui_build.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ui_build.UIBuildError, match="SSL_CERT_FILE"):
        ui_build._download("https://nodejs.org/dist/x.tar.xz", ui_build.Path("/tmp/x"))


def test_an_unrelated_download_failure_is_not_blamed_on_certificates(monkeypatch):
    """A timeout must not come with certificate advice attached."""
    monkeypatch.setattr(ui_build, "ensure_ca_bundle", lambda: None)
    monkeypatch.setattr(
        ui_build.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    with pytest.raises(ui_build.UIBuildError) as caught:
        ui_build._download("https://nodejs.org/dist/x.tar.xz", ui_build.Path("/tmp/x"))

    assert "SSL_CERT_FILE" not in str(caught.value)


def test_the_slack_listener_and_the_node_download_share_one_implementation():
    """Two callers, one fix. The first occurrence was fixed in a private helper
    the second knew nothing about, which is how it happened twice."""
    assert listener.ensure_ca_bundle is tls.ensure_ca_bundle
    assert ui_build.ensure_ca_bundle is tls.ensure_ca_bundle
