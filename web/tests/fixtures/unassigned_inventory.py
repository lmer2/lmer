"""Produce isolated HTTP inventory responses for the mounted recovery UI test."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path[:0] = [str(Path(__file__).resolve().parents[3] / "src"), str(Path(__file__).resolve().parents[3])]

from fastapi.testclient import TestClient
import pytest

from lmer_platform import api, config, inventory, projects, registry, store, transcripts
from tests.conftest import strip_lmer_env
from tests.test_project_recovery_reads import retained_session


with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
    strip_lmer_env(patch)
    patch.setattr(store, "PLATFORM_DIR", directory)
    patch.setattr(transcripts, "TRANSCRIPT_ROOT", str(Path(directory) / "transcripts"))
    patch.setattr(api, "pull", lambda *a, **kw: SimpleNamespace(to_dict=lambda: {"present": True, "healthy": True}))
    patch.setattr(api, "resolve_run_dir", lambda *a, **kw: None)
    patch.setattr(inventory, "session_activity", lambda *a, **kw: None)
    legacy = ("s-legacy-assistant", "s-legacy-worker")
    original = {}
    for identity, kind in zip(legacy, ("assistant", "worker")):
        original.update(retained_session(identity, kind))
    projects.migrate()
    alpha = projects.create("Alpha")
    for identity, pid in (("s-alpha-failure", 99999999), ("s-alpha-current", os.getpid())):
        registry.register(identity, pid=pid, kind="assistant",
                          run={"host": None, "project": None, "slug": "orchestrate-fleet"})
        projects.bind_session(identity, alpha["id"])
    with TestClient(api.create_app(config.load(), "fixture-operator"),
                    headers={"Authorization": "Bearer fixture-operator"}) as client:
        state = client.get("/api/state")
        project_list = client.get("/api/projects")
        scoped = client.get("/api/state", params={"workspace_id": alpha["id"]})
        assert state.status_code == project_list.status_code == scoped.status_code == 200
        assert len(state.json()["runs"]) == 4 and len(state.json()["attention"]) == 3
        assert len(scoped.json()["runs"]) == 2
        reads = {}
        for identity in legacy:
            reads[identity] = {}
            for endpoint in ("messages", "log", "ask"):
                response = client.get(f"/api/sessions/{identity}/{endpoint}")
                assert response.status_code == 200, response.text
                reads[identity][endpoint] = {"status": response.status_code, "body": response.json()}
        assert all(projects.session_home(identity) is None for identity in legacy)
        assert all(path.read_bytes() == data for path, data in original.items())
        print(json.dumps({"state": state.json(), "projects": project_list.json(), "alpha": alpha["id"], "reads": reads}))
