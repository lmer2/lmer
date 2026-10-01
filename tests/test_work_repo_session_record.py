"""What the session-metadata scrubber guarantees (work_repo.session_record).

Three rules, each independent of the others: a value under a secret-looking
key is redacted; a string containing a value this process holds in a
secret-named env var is redacted; and a token-SHAPED string is redacted even
under a benign key and even when this process does not hold it — the case
operator-supplied ``session_metadata`` on the platform API can produce.
"""
import json

import pytest

from tests.conftest import strip_lmer_env
from work_repo import session_record

REDACTED = "***REDACTED***"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def test_the_reviewers_probe(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-REALSECRETVALUE1234")
    monkeypatch.setenv(session_record.METADATA_ENV, json.dumps({
        "note": "token is glpat-REALSECRETVALUE1234 here",
        "api_key": "anything", "Authorization": "Bearer x", "cookie": "c=1",
        "nested": [{"password": "p"}, "plain", "glpat-NOTINENV-ABCDEF"],
    }))
    scrubbed = session_record.provided_metadata()
    assert scrubbed["note"] == f"token is {REDACTED} here"
    assert scrubbed["api_key"] == scrubbed["Authorization"] == scrubbed["cookie"] == REDACTED
    assert scrubbed["nested"][0]["password"] == REDACTED
    assert scrubbed["nested"][1] == "plain"
    # Token-shaped, under a benign key, not in this process's env: was kept.
    assert scrubbed["nested"][2] == REDACTED
    assert "NOTINENV" not in json.dumps(scrubbed)


@pytest.mark.parametrize("token", [
    "glpat-NOTINENV-ABCDEF",
    "ghp_abcdEFGH1234", "gho_abcdEFGH1234", "ghs_abcdEFGH1234",
    "github_pat_11ABCDEFG0abcdefgh",
    "AKIAIOSFODNN7EXAMPLE", "ASIAIOSFODNN7EXAMPLE",
    "xoxb-1234-5678-abcdef", "xoxa-1234-5678-abcdef", "xoxp-1234-5678-abcdef",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n-----END OPENSSH PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----\nMIIEvQ",
])
def test_a_token_shape_is_redacted_under_a_benign_key(token):
    scrubbed = session_record.scrub_metadata(
        {"comment": f"see {token} for access", "list": [token], "deep": {"why": token}}
    )
    dumped = json.dumps(scrubbed)
    for fragment in (token.splitlines()[0], token.splitlines()[-1]):
        assert fragment not in dumped
    assert scrubbed["comment"].startswith("see ") and REDACTED in scrubbed["comment"]
    assert scrubbed["list"] == [REDACTED] and scrubbed["deep"]["why"] == REDACTED


def test_ordinary_launch_metadata_is_left_alone():
    metadata = {
        "uid": "0123456789abcdef0123456789abcdef", "operator": "someone",
        "hostname": "build-host.example.test", "model": "claude-fable-5-1",
        "region": "asia-pacific", "note": "AKIA is an AWS key-id prefix; ghp_ is GitHub's",
        "config": {"preset": "review", "agents": ["coder"], "ports": [8080]},
        "launcher": {"pid": 4242, "started_at": "2026-09-18T05:00:00+00:00"},
    }
    assert session_record.scrub_metadata(metadata) == metadata


# A redactor's expensive error is the miss. Where a token may START is not
# enumerable — it follows an ANSI colour code, a JSON-escaped newline, a
# hyphen, an underscore, a letter, a digit, a percent-escape — so nothing here
# may depend on what PRECEDES a prefix. A left boundary was tried once to stop
# a hostname reading as `lmer-ta***REDACTED***`; it let every case below
# through, in this scrubber and in `redact_secrets` (dozens of call sites:
# logs, state, events, commit subjects). Over-redacting a hostname is cosmetic
# and stays; false positives may only be reduced by constraining what FOLLOWS
# the prefix.
GITHUB = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
ANTHROPIC = "sk-ant-api03-" + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7" * 3
GITLAB = "glpat-" + "A1b2C3d4E5f6G7h8I9j0"
ESCAPE_CONTEXTS = {
    "slug-hyphen": "execute-{t}",
    "json-escaped-newline": "error:\\n{t}",
    "ansi-colour": "Token: \x1b[1m{t}\x1b[0m",
    "underscore-upper": "MY_PREFIX_{t}",
    "underscore-lower": "key_{t}",
    "letters": "token{t}",
    "digit": "v2{t}",
    "percent-escape": "key%3D{t}",
    "start": "{t}",
    "equals": "TOKEN={t}",
}


@pytest.mark.parametrize("token", [GITHUB, ANTHROPIC, GITLAB], ids=["github", "anthropic", "gitlab"])
@pytest.mark.parametrize("context", sorted(ESCAPE_CONTEXTS))
def test_no_left_context_lets_a_token_through(context, token):
    from work_repo import utils

    text = ESCAPE_CONTEXTS[context].format(t=token)
    assert token not in utils.redact_secrets(text, []), "redact_secrets let it through"
    assert token not in utils.redact_token_shapes(text), "redact_token_shapes let it through"
    scrubbed = session_record.scrub_metadata({"note": text, "deep": [{"why": text}]})
    assert token not in json.dumps(scrubbed)


@pytest.mark.parametrize("text", [
    "KEY_AKIAIOSFODNN7EXAMPLE", "idAKIAIOSFODNN7EXAMPLE_suffix", "\x1b[1mASIAIOSFODNN7EXAMPLE\x1b[0m",
])
def test_an_aws_key_id_is_caught_whatever_surrounds_it(text):
    from work_repo import utils

    assert "IOSFODNN7EXAMPLE" not in utils.redact_token_shapes(text)
