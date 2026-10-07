"""Isolated HTTP/Chromium restart regression; uses disposable sleeping launchers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import signal
import threading
from types import SimpleNamespace
import urllib.request
from unittest.mock import patch

from websockets.asyncio.client import connect

import projects as fixture
from lmer_platform import api, assistant, assistant_history, config, inventory, projects, registry, runs, spawn


def end_session(session):
    os.killpg(registry.read_session(session)["pid"], signal.SIGTERM)
    assert spawn.wait_for_exit_recorded(session)


def prepare(alpha, beta, settings):
    for run in runs.list_tracked():
        runs.forget(run.host, run.project, run.slug)
    projects.update(alpha["id"], {"name": "Alpha"})
    alpha["name"] = "Alpha"
    config.update_stored({"max_live_assistants": 4})
    for project in (alpha, beta):
        projects.request_start(project["id"])
    projects.reconcile(settings)
    old = projects.status(alpha["id"])["session_id"]
    assistant_history.retain(old, {"id": "retained", "kind": "message", "text": "Retained Alpha decision",
                                  "source": {"channel": "agent"}})
    end_session(old)
    projects.request_start(alpha["id"])
    projects.reconcile(settings)
    with projects.database() as db:
        db.execute("UPDATE projects SET retry_at=0 WHERE id=?", (alpha["id"],))
    projects.reconcile(settings)
    alpha["previous_session"] = old


async def probe(chrome, output, url, secret, alpha, beta, settings):
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{chrome}/json/list", timeout=5))
    page = next(target for target in targets if target["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as connection:
        browser = fixture.Browser(connection)
        browser.output = output
        await browser.call("Runtime.enable")
        await browser.call("Network.enable")
        await browser.call("Network.setExtraHTTPHeaders", headers={"Authorization": f"Bearer {secret}"})
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.call("Page.navigate", url=url)
        group = "document.querySelector('.project-nav[aria-label=\"Alpha\"]')"
        await browser.wait(group + "?.querySelectorAll('.run-row').length === 1")
        assert await browser.evaluate(group + ".textContent.includes('running')")
        assert not await browser.evaluate("document.body.textContent.includes('died unexpectedly')")
        assert await browser.evaluate("document.querySelectorAll('main .run-row').length") == 2
        await browser.shot(output, "recovered-all-projects")
        for project in (alpha, beta):
            await browser.click(project["name"], '[role="tab"]')
            await browser.wait("document.querySelector('.project-identity').textContent.includes(" + json.dumps(project["name"]) + ")")
            assert await browser.evaluate("document.querySelectorAll('main [aria-label=\"talk to uber lmer\"]').length") == 1
            assert await browser.evaluate("document.querySelector('header button[aria-label^=\"uber lmer\"]').getAttribute('aria-label').includes('running')")
        before = projects.status(alpha["id"])
        supervisor = projects.Supervisor(settings)
        supervisor.stop()
        projects.Supervisor(settings).first_start()
        assert projects.status(alpha["id"])["session_id"] == before["session_id"]
        assert projects.status(alpha["id"])["generation"] == before["generation"]

        end_session(before["session_id"])
        projects.reconcile(settings)
        with projects.database() as db:
            db.execute("UPDATE projects SET retry_at=0 WHERE id=?", (alpha["id"],))
        entered, release = threading.Event(), threading.Event()
        original = assistant.start

        def held_start(*args, **kwargs):
            entered.set()
            assert release.wait(40)
            return original(*args, **kwargs)

        with patch.object(assistant, "start", held_start), ThreadPoolExecutor(max_workers=1) as pool:
            launching = pool.submit(projects.reconcile, settings)
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                await browser.call("Page.reload")
                await browser.wait(group + "?.querySelector('.project-assistant').textContent.includes('starting')")
                assert await browser.evaluate(group + ".querySelectorAll('.run-row').length") == 0
                await browser.shot(output, "replacement-starting")
            finally:
                release.set()
            launching.result(timeout=10)
        await browser.call("Page.reload")
        await browser.wait(group + "?.querySelectorAll('.run-row').length === 1")
        await browser.wait(group + ".querySelector('.project-assistant').textContent.includes('running')")
        current = projects.status(alpha["id"])["session_id"]
        assert current != before["session_id"]
        await browser.shot(output, "replacement-running")

        end_session(current)
        await browser.call("Page.reload")
        await browser.wait(group + "?.querySelector('.project-assistant').textContent.includes('failed')")
        assert await browser.evaluate(group + ".querySelectorAll('.run-row').length") == 1
        assert await browser.evaluate(group + ".textContent.includes('died unexpectedly')")
        await browser.click("Alpha", '[role="tab"]')
        await browser.wait("document.querySelector('main').textContent.includes('died unexpectedly')")
        assert await browser.evaluate("document.querySelector('header button[aria-label^=\"uber lmer\"]').getAttribute('aria-label').includes('crashed')")
        await browser.shot(output, "current-failure")
        await browser.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
        await browser.wait("document.querySelector('[aria-label=\"show the run list\"]')")
        await browser.click("show the run list")
        await browser.wait("document.querySelector('.v-navigation-drawer--left').getBoundingClientRect().left >= 0")
        assert await browser.evaluate(group + ".querySelectorAll('.run-row').length") == 1
        await browser.shot(output, "current-failure-phone")
        history = assistant_history.page(alpha["previous_session"], since=0)
        assert any(row["text"] == "Retained Alpha decision" for row in history["messages"])
        assert registry.read_session(alpha["previous_session"])
        assert not browser.errors, browser.errors
        (output / "browser-results.json").write_text(json.dumps({
            "result": "pass", "viewports": ["1440x1000", "390x844"],
            "checks": ["recovered assistant replaces retired crash in sidebar and All projects",
                       "project main views and header reflect current assistant", "clean restart adopts survivor",
                       "launch gap shows starting without stale crash", "replacement becomes running",
                       "current crash remains visible in sidebar, main view and header", "phone failure visible",
                       "registry and conversation history preserved", "no runtime exceptions"],
            "limits": "Real local HTTP and Chromium with temporary stores and sleeping launcher processes; no live platform or container runtime.",
        }, indent=2) + "\n")


if __name__ == "__main__":
    with patch.object(api, "pull", lambda *a, **kw: SimpleNamespace(to_dict=lambda: {
        "present": True, "healthy": True,
    })), patch.object(inventory, "session_activity", lambda *a, **kw: None):
        fixture.main(probe, prepare=prepare, state_builder=api.build_state)
