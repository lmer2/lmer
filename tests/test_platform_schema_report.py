"""The validator-backed half of #374's schema views (spec §13.F, task 80).

``GET /api/workrepo/schema`` answers ``{form, tier, document, buckets, note}``
from ONE in-process ``lmer-schema validate --json`` run over the daemon's
mirror (:mod:`lmer_platform.schema_report`). Pinned here:

- the document IS the CLI's JSON, field for field, for the same flags —
  including the ``--plexus-repo-url`` the module arms from the scrubbed
  work-repo URL, and with no token anywhere in the reply;
- the host bucket is listed from the document's own ``tree.host-bucket``
  finding with its ``work adopt <host>/<project> <short-name>`` line;
- a legacy tree answers ``form: "legacy"`` with no document, and the validator
  is never called; an absent mirror answers ``form: None``;
- a broken manifest is a 200 carrying ``unusable`` and exit code 2;
- the cache: a hit on an unchanged HEAD within the expiry, a miss on a new
  HEAD, nothing cached without one — and the frozen-clock case (astra PR7)
  where the same HEAD and the same record bytes cross the 14-day stale window,
  and ``record.session-stale`` appears on ``?refresh=1`` and after expiry;
- the route is behind the secret, listed in ``GET /api``, and never pulls;
- ``build_state``'s ``mirror.form`` is right for both fixtures;
- ``workrepo.py`` still never imports the validator;
- the replies match the goldens ``web/tests/fixtures/{schema-form,legacy}/
  workrepo-schema.json`` (``REGEN_SCHEMA_GOLDENS=1`` rewrites them).

The tree is :func:`tests.test_platform_schema_views.build_sessions_mirror`,
imported, plus one unnamed repo planted under a dotted host dir.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, schema_report, store, workrepo
from lmer_platform import config as cfg
from lmer_schema import cli as schema_cli

# The #374 fixture builder and golden helpers, imported rather than copied:
# both suites must read the same tree, or the two halves of one view would be
# pinned against different worlds.
from tests.test_platform_schema_views import (  # noqa: F401 - fixtures by import
    GOLDEN_DIR,
    PINNED_NOW,
    _clean_env,
    assert_golden,
    build_legacy_mirror,
    build_sessions_mirror,
    platform_root,
)

SECRET = "test-secret-value"
TOKEN = "glpat-supersecrettoken12345678"
TOKENISED_URL = f"https://oauth2:{TOKEN}@git.example.test/acme/work.git"
CLEAN_URL = "https://git.example.test/acme/work.git"
BUCKET = "gitlab.example.com/unnamed/widget"
SCHEMA_GOLDEN = GOLDEN_DIR / "schema-form" / "workrepo-schema.json"
LEGACY_GOLDEN = GOLDEN_DIR / "legacy" / "workrepo-schema.json"
#: The fixture's in-progress record was last updated at 2026-09-02T12:00Z, so
#: it crosses the validator's 14-day window at 2026-09-16T12:00Z.
STALE_AT = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def plant_bucket(root: Path) -> Path:
    """An unnamed repo's work under a dotted host dir: project-shaped, unregistered."""
    bucket = root.joinpath(*BUCKET.split("/"))
    (bucket / "_runs").mkdir(parents=True)
    (bucket / "_info").mkdir()
    (bucket / "_info" / "notes.md").write_text("# unnamed\n", encoding="utf-8")
    return bucket


@pytest.fixture(autouse=True)
def _fresh_cache():
    schema_report.clear_cache()
    yield
    schema_report.clear_cache()


@pytest.fixture
def mirror(tmp_path):
    root = build_sessions_mirror(tmp_path / "mirror")
    plant_bucket(root)
    return root


@pytest.fixture
def legacy(tmp_path):
    return build_legacy_mirror(tmp_path / "legacy")


@pytest.fixture
def frozen(monkeypatch):
    """A settable clock for the report's validation and its cache expiry."""
    moment = {"now": PINNED_NOW}
    monkeypatch.setattr(schema_report, "_clock", lambda: moment["now"].timestamp())
    return moment


@pytest.fixture
def counted(monkeypatch):
    """Count validator runs, still running the real one."""
    calls = []
    real = schema_report.validate

    def counting(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(schema_report, "validate", counting)
    return calls


def _config(root, url=TOKENISED_URL):
    return cfg.load({"work_repo_url": url, "work_repo_mirror": str(root)})


def _client(root, url=TOKENISED_URL) -> TestClient:
    def fake_state(config, *, force_pull=False):
        return {"runs": [], "attention": []}

    return TestClient(api.create_app(_config(root, url), SECRET, state_builder=fake_state))


def _bearer():
    return {"Authorization": f"Bearer {SECRET}"}


def _schema(client, **params):
    return client.get("/api/workrepo/schema", params=params, headers=_bearer())


def _set_head(sha):
    workrepo._save_state(head_sha=sha)


def _codes(document):
    return [finding["code"] for finding in document["findings"]]


def _cli_document(capsys, root, *flags):
    capsys.readouterr()
    code = schema_cli.main(["validate", str(Path(root).resolve()), "--json", *flags])
    document = json.loads(capsys.readouterr().out)
    assert code == document["exit_code"]
    return document


# --- the document is the CLI's ----------------------------------------------------

def test_the_document_is_the_clis_json_field_for_field(platform_root, mirror, capsys):
    reply = _schema(_client(mirror))
    assert reply.status_code == 200, reply.text
    body = reply.json()

    assert body["form"] == "schema"
    assert body["tier"] == "schema"
    assert body["document"] == _cli_document(capsys, mirror, "--plexus-repo-url", CLEAN_URL)


def test_without_a_work_repo_url_no_guard_flag_is_passed(platform_root, mirror, capsys):
    body = _schema(_client(mirror, url=None)).json()
    assert body["document"] == _cli_document(capsys, mirror)


def test_the_tokenised_work_repo_url_never_reaches_the_reply(platform_root, mirror, counted):
    reply = _schema(_client(mirror))
    assert TOKEN not in reply.text
    assert counted[0]["plexus_repo_url"] == CLEAN_URL


# --- buckets -------------------------------------------------------------------------

def test_the_bucket_is_listed_with_its_adopt_line(platform_root, mirror):
    body = _schema(_client(mirror)).json()
    assert body["buckets"] == [{
        "path": BUCKET,
        "message": next(
            finding["message"] for finding in body["document"]["findings"]
            if finding["code"] == "tree.host-bucket"
        ),
        "adopt_command": f"work adopt {BUCKET} <short-name>",
    }]


def test_the_short_name_is_a_placeholder_never_derived(platform_root, mirror):
    """D9: names are never derived from a path — not ``widget``, not ``unnamed``."""
    (bucket,) = _schema(_client(mirror)).json()["buckets"]
    assert bucket["adopt_command"].split()[-1] == schema_report.ADOPT_PLACEHOLDER


# --- legacy, absent, broken -----------------------------------------------------------

def test_a_legacy_tree_is_never_validated(platform_root, legacy, monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - failing is the assertion
        raise AssertionError("a legacy tree was validated")

    monkeypatch.setattr(schema_report, "validate", refuse)
    reply = _schema(_client(legacy))
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert (body["form"], body["tier"], body["document"], body["buckets"]) == (
        "legacy", "legacy", None, [],
    )


def test_an_absent_mirror_has_no_form(platform_root, tmp_path):
    body = _schema(_client(tmp_path / "nowhere")).json()
    assert (body["form"], body["tier"], body["document"], body["buckets"]) == (
        None, None, None, [],
    )


def test_a_broken_manifest_is_a_200_carrying_exit_code_2(platform_root, mirror):
    (mirror / "_plexus.yaml").write_text("schema: [unclosed\n", encoding="utf-8")
    reply = _schema(_client(mirror))
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["form"] == "schema"
    assert body["document"]["exit_code"] == 2
    assert body["document"]["unusable"]
    assert body["buckets"] == []


# --- the cache ---------------------------------------------------------------------

def test_an_unchanged_head_within_the_expiry_is_a_cache_hit(
    platform_root, mirror, frozen, counted,
):
    _set_head("a" * 40)
    client = _client(mirror)
    first = _schema(client).json()
    frozen["now"] += timedelta(seconds=schema_report.CACHE_SECONDS - 1)
    second = _schema(client).json()
    assert len(counted) == 1
    assert first == second


def test_a_new_head_is_a_miss(platform_root, mirror, frozen, counted):
    client = _client(mirror)
    _set_head("a" * 40)
    _schema(client)
    _set_head("b" * 40)
    _schema(client)
    assert len(counted) == 2


def test_with_no_head_nothing_is_cached(platform_root, mirror, frozen, counted):
    client = _client(mirror)
    _schema(client)
    _schema(client)
    assert len(counted) == 2


def test_refresh_bypasses_the_cache(platform_root, mirror, frozen, counted):
    _set_head("a" * 40)
    client = _client(mirror)
    _schema(client)
    _schema(client, refresh="1")
    assert len(counted) == 2


def test_a_record_crossing_the_stale_window_at_the_same_head(
    platform_root, mirror, frozen, counted,
):
    """astra PR7: the same HEAD and the same record bytes owe a different
    document once the clock crosses 14 days — a revision-only key would serve
    the old one forever."""
    _set_head("a" * 40)
    client = _client(mirror)

    frozen["now"] = STALE_AT - timedelta(minutes=2)
    assert "record.session-stale" not in _codes(_schema(client).json()["document"])

    frozen["now"] = STALE_AT + timedelta(minutes=3)
    cached = _schema(client).json()["document"]
    assert "record.session-stale" not in _codes(cached)  # still within the expiry
    assert len(counted) == 1

    refreshed = _schema(client, refresh="1").json()["document"]
    assert "record.session-stale" in _codes(refreshed)

    schema_report.clear_cache()
    frozen["now"] = STALE_AT - timedelta(minutes=2)
    _schema(client)
    frozen["now"] = STALE_AT - timedelta(minutes=2) + timedelta(
        seconds=schema_report.CACHE_SECONDS + 1,
    )
    expired = _schema(client).json()["document"]
    assert "record.session-stale" in _codes(expired)


# --- guard, index, no pull --------------------------------------------------------------

def test_the_route_needs_the_shared_secret(platform_root, mirror):
    assert _client(mirror).get("/api/workrepo/schema").status_code == 401


def test_the_route_is_listed_in_the_api_index(platform_root, mirror):
    assert "GET  /api/workrepo/schema" in _client(mirror).get("/api", headers=_bearer()).text


def test_the_route_never_pulls(platform_root, mirror, monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - failing is the assertion
        raise AssertionError("GET /api/workrepo/schema pulled the mirror")

    monkeypatch.setattr(api, "pull", refuse)
    monkeypatch.setattr(workrepo, "pull", refuse)
    assert _schema(_client(mirror)).status_code == 200


# --- mirror.form on the fleet payload ---------------------------------------------------

def test_build_state_carries_the_mirror_form(platform_root, mirror, legacy, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - failing is the assertion
        raise AssertionError("the fleet poll ran the validator")

    monkeypatch.setattr(schema_report, "validate", refuse)
    for root, form in ((mirror, "schema"), (legacy, "legacy"), (tmp_path / "nowhere", None)):
        payload = api.build_state(cfg.load({"work_repo_mirror": str(root)}))
        assert payload["mirror"]["form"] == form


# --- the fleet read path stays validator-free ---------------------------------------------

def test_workrepo_never_imports_the_validator():
    source = Path(workrepo.__file__).read_text(encoding="utf-8")
    assert "lmer_schema.checks" not in source
    assert "lmer_schema.cli" not in source


# --- goldens -------------------------------------------------------------------------------

def test_the_schema_form_reply_is_the_checked_in_golden(platform_root, mirror, frozen):
    reply = _schema(_client(mirror))
    assert reply.status_code == 200, reply.text
    assert_golden(SCHEMA_GOLDEN, reply.json())


def test_the_legacy_reply_is_the_checked_in_golden(platform_root, legacy):
    reply = _schema(_client(legacy))
    assert reply.status_code == 200, reply.text
    assert_golden(LEGACY_GOLDEN, reply.json())
