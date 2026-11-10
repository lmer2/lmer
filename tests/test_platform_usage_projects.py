"""Usage routes with projects on (#315, #647): scoped like /api/state."""

from __future__ import annotations

from lmer_platform import config, projects, usage_api, usage_live
from lmer_platform import project_context as scope
from tests.test_platform_project_routing import client  # noqa: F401
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_platform_usage_api import NOW, FakeLive, _event

HOST, REPO = "forge.test", "same/repository"


def _two_projects(client):  # noqa: F811
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    for slug, home in (("alpha-run", alpha), ("beta-run", beta)):
        adopted = client.post("/api/runs/adopt", json={"host": HOST, "project": REPO, "slug": slug,
                                                       "workspace_id": home["id"]})
        assert adopted.status_code == 200, adopted.text
    return alpha, beta


def _burning(monkeypatch):
    live = FakeLive({
        "s-alpha1": {"run": (HOST, REPO, "alpha-run"), "events": [_event("a", 1, output=500)]},
        "s-beta1": {"run": (HOST, REPO, "beta-run"), "events": [_event("b", 1, output=900)]},
    })
    monkeypatch.setattr(usage_live, "_CURRENT", live)
    monkeypatch.setattr(usage_api, "_now", lambda: NOW)


def _slugs(reply):
    assert reply.status_code == 200, reply.text
    return sorted(row["slug"] for row in reply.json()["top_runs"])


def test_an_assistant_sees_only_its_own_projects_runs_and_spend(client, monkeypatch):  # noqa: F811
    alpha, beta = _two_projects(client)
    _burning(monkeypatch)
    with scope.bound(alpha["id"]):
        token = config.active_assistant_credential()
    mine = client.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
    assert _slugs(mine) == ["alpha-run"]
    assert mine.json()["totals"]["output"] == 500
    assert _slugs(client.get("/api/usage")) == ["alpha-run", "beta-run"]
    assert _slugs(client.get("/api/usage", params={"workspace_id": beta["id"]})) == ["beta-run"]


def test_the_rollup_is_the_operators_all_projects_view(client):  # noqa: F811
    alpha, _ = _two_projects(client)
    reply = client.get("/api/usage/rollup", params={"project": f"{HOST}/{REPO}"})
    assert reply.status_code == 200, reply.text
    with scope.bound(alpha["id"]):
        token = config.active_assistant_credential()
    refused = client.get("/api/usage/rollup", params={"project": f"{HOST}/{REPO}"},
                         headers={"Authorization": f"Bearer {token}"})
    assert refused.status_code == 403, refused.text
    assert projects.enabled()
