"""Graph-group editor acceptance against Chromium and isolated platform HTTP/stores.

Run: uv run python web/tests/browser/fleet_groups.py CHROMIUM OUTPUT
"""
import asyncio
import json
import urllib.request

from websockets.asyncio.client import connect

import projects as fixture
from project_controls import input_text
from lmer_platform import api, config, projects, runs
from lmer_platform import project_context as scope


def prepare(alpha, beta, _settings):
    for project, name in ((alpha, "Example Alpha"), (beta, "Example Beta")):
        projects.update(project["id"], {"name": name})
        project["name"] = name
        with scope.bound(project["id"]):
            runs.forget("forge.test", "shared/repository", "alpha-question" if project == alpha else "beta-question")
            for repository in (["team/widget", "tools/team-site"] if project == alpha else ["team/other"]):
                runs.track("forge.test", repository, "example-work")


def inventory(settings):
    rows = [{**row.to_dict(), "state": "running", "label": row.project, "live": False, "attention": None}
            for row in runs.list_tracked()]
    return {"config": api._config_summary(settings, {}), "runs": rows, "attention": [], "sessions": [],
            "slots": [], "relations": [], "totals": {"runs": len(rows), "attention": 0, "live": 0}}


async def probe(chrome, output, url, secret, alpha, beta, settings):
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{chrome}/json/list", timeout=10))
    page = next(target for target in targets if target["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as connection:
        browser = fixture.Browser(connection)
        browser.output = output
        await browser.call("Runtime.enable")
        await browser.call("Network.enable")
        await browser.call("Network.setExtraHTTPHeaders", headers={"Authorization": f"Bearer {secret}"})
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.call("Page.navigate", url=url)
        await browser.wait("document.querySelectorAll('.project-nav').length === 3")
        await browser.click(alpha["name"], '[role="tab"]')
        await browser.click("read the fleet as a constellation map")
        await browser.wait("document.querySelectorAll('.fleet-map__core').length === 2")
        assert not await browser.evaluate("document.querySelector('.fleet-map__group-ring') !== null")
        await browser.click("Platform settings")
        await browser.wait("document.querySelector('.fleet-groups')?.textContent.includes('No graph groups')")
        await browser.click("Add group")
        await browser.click("Save groups")
        await browser.wait("document.querySelector('.fleet-groups .v-alert')")
        await browser.shot(output, "groups-validation-desktop")
        await input_text(browser, "document.querySelector('.fleet-group-row input')", "Team")
        await input_text(browser, "document.querySelector('.fleet-group-row textarea:not(.v-textarea__sizer)')", "team/\ntools/team-")
        await browser.click("Add group")
        await input_text(browser, "document.querySelectorAll('.fleet-group-row input')[1]", "Widget")
        await input_text(browser, "document.querySelectorAll('.fleet-group-row textarea:not(.v-textarea__sizer)')[1]", "widget")
        await browser.click("Move group 2 up")
        assert await browser.evaluate("document.querySelector('.fleet-group-row input').value") == "Widget"
        await browser.click("Move group 1 down")
        await browser.click("Save groups")
        await browser.wait("document.querySelector('.fleet-groups [role=status]')")
        expected = [{"name": "Team", "patterns": ["team/", "tools/team-"]}, {"name": "Widget", "patterns": ["widget"]}]
        assert config.load().fleet_groups == expected
        await browser.evaluate("document.querySelector('.fleet-groups').scrollIntoView({block:'start'})")
        await asyncio.sleep(.3)
        await browser.shot(output, "groups-editor-desktop")
        await browser.click("back to the fleet")
        await browser.wait("document.querySelector('.fleet-map__group')?.textContent === 'Team'")
        assert await browser.evaluate("document.querySelectorAll('.fleet-map__core').length") == 2
        assert not await browser.evaluate("document.querySelector('.fleet-map').textContent.includes('team/other')")
        await browser.shot(output, "groups-map-desktop")

        # A second settings client writes while the graph stays mounted; the normal poll updates it.
        changed = [{**expected[0], "name": "Renamed"}, expected[1]]
        result = await browser.evaluate("fetch('api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(" + json.dumps({"fleet_groups": changed}) + ")}).then(r => r.status)")
        assert result == 200, result
        await browser.wait("document.querySelector('.fleet-map__group')?.textContent === 'Renamed'")
        await browser.call("Page.reload")
        await browser.wait("document.querySelector('.fleet-map__group')?.textContent === 'Renamed'")
        assert await browser.evaluate("document.querySelectorAll('.fleet-map__core').length") == 2

        for width in (390, 320):
            await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=844, deviceScaleFactor=1, mobile=True)
            await browser.click("Platform settings")
            await browser.wait("document.querySelector('.fleet-group-row input')?.value === 'Renamed'")
            await browser.evaluate("document.querySelector('.fleet-groups').scrollIntoView({block:'start'})")
            await asyncio.sleep(.3)
            assert not await browser.evaluate("document.documentElement.scrollWidth > innerWidth"), width
            assert await browser.evaluate("[...document.querySelectorAll('.fleet-group-row input,.fleet-group-row textarea:not(.v-textarea__sizer),.fleet-group-actions button')].every(e => { const r=e.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })"), width
            await browser.shot(output, f"groups-editor-{width}")
            await browser.click("back to the fleet")
            await browser.wait("document.querySelector('.fleet-map__group')?.textContent === 'Renamed'")

        await browser.click("Platform settings")
        await browser.wait("document.querySelector('.fleet-group-row input')?.value === 'Renamed'")
        await input_text(browser, "document.querySelector('.fleet-group-row input')", "Team")
        await browser.click("Remove group 2")
        await browser.click("Save groups")
        await browser.wait("document.querySelector('.fleet-groups [role=status]')")
        assert config.load().fleet_groups == expected[:1]
        await browser.click("Remove group 1")
        await browser.click("Save groups")
        await browser.wait("document.querySelector('.fleet-groups [role=status]')")
        assert config.load().fleet_groups == []
        await browser.click("back to the fleet")
        await browser.wait("document.querySelectorAll('.fleet-map__core').length === 2 && !document.querySelector('.fleet-map__group-ring')")
        assert not browser.errors, browser.errors
    (output / "browser-results.json").write_text(json.dumps({
        "result": "pass", "viewports": ["1440x1000", "390x844", "320x844"], "runtime_errors": browser.errors,
        "checks": ["empty default repository clusters", "invalid save shown beside editor", "add/edit/reorder/save through HTTP",
                   "grouping reaches graph after save", "second-client save reaches mounted graph through normal poll",
                   "browser reload retains rules", "selected named project excludes other runs", "narrow controls fit viewport",
                   "edit/remove/clear persists and restores repository clusters"],
        "limits": "Actual built UI and platform HTTP/config; isolated temporary stores and fixture inventory. No live daemon or external transports.",
    }, indent=2) + "\n")


if __name__ == "__main__":
    fixture.main(probe, prepare=prepare, state_builder=inventory)
