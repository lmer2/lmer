"""Read-only slot visibility through isolated HTTP and Chromium."""
import json
import urllib.request

from websockets.asyncio.client import connect

import projects as fixture
from lmer_platform import projects, runs

state = {"phase": "held"}


def prepare(alpha, beta, _settings):
    projects.update(alpha["id"], {"name": "Example Alpha"})
    projects.update(beta["id"], {"name": "Example Beta"})
    state.update(alpha=alpha["id"], beta=beta["id"])


def inventory(_settings):
    rows = [{**run.to_dict(), "label": run.slug, "state": "running", "live": True,
             "session": {"id": "s-alpha" if run.slug == "alpha-question" else "s-beta"}} for run in runs.list_tracked()]
    def slot(name, **values):
        return {"name": name, "state": "free", "applicable_projects": [state["alpha"], state["beta"]],
                "needs_project_assignment": False, "occupants": [], "service_occupants": [], **values}
    held = state["phase"] == "held"
    slots = [
        slot("Alpha service", state="occupied", applicable_projects=[], occupants=[{"session_id": "s-alpha"}]),
        slot("Shared service", state="occupied" if held else "free", occupants=[{"session_id": "s-beta"}] if held else []),
        slot("Ready service"),
        slot("Overlapping service", state="occupied" if held else "free",
             service_occupants=[{"session_id": "s-beta", "slot": "Shared service"}] if held else []),
        slot("Offline service", state="service_down", reason="Service is not running"),
        slot("Beta only", applicable_projects=[state["beta"]]),
    ] if state["phase"] != "empty" else []
    return {"runs": rows, "attention": [], "slots": slots, "relations": [], "totals": {"runs": len(rows)}}


async def probe(chrome, output, url, secret, alpha, beta, _settings):
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{chrome}/json/list"))
    page = next(target for target in targets if target["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as connection:
        browser = fixture.Browser(connection)
        browser.output = output
        await browser.call("Runtime.enable")
        await browser.call("Network.enable")
        await browser.call("Network.setExtraHTTPHeaders", headers={"Authorization": f"Bearer {secret}"})
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.call("Page.navigate", url=url)
        region = "document.querySelector('.project-slots')"
        await browser.wait(region + "?.querySelectorAll('.project-slot').length === 3")
        assert await browser.evaluate(region + ".textContent.includes('Example Alpha') && " + region + ".textContent.includes('Example Beta')")
        assert not await browser.evaluate(region + ".textContent.includes('Ready service')")
        assert not await browser.evaluate(region + ".querySelector('button, a, input')")
        await browser.shot(output, "all-projects-held-desktop")
        await browser.click("Example Alpha", '[role="tab"]')
        await browser.wait(region + ".querySelectorAll('.project-slot').length === 5")
        assert not await browser.evaluate(region + ".textContent.includes('Beta only')")
        shared = "document.querySelector('[aria-label=\"Service slot Shared service\"]')"
        assert await browser.evaluate(shared + ".textContent.includes('Example Beta')")
        assert await browser.evaluate(shared + ".textContent.includes('Unavailable for a new run.')")
        assert await browser.evaluate(region + ".textContent.includes('Held allocation retained; not assigned for new runs.')")
        assert await browser.evaluate("[..." + region + ".querySelectorAll('.slot-availability')].filter(e => e.textContent === 'Available now').length") == 1
        await browser.shot(output, "selected-alpha-desktop")
        projects.update(beta["id"], {"name": "Example Beta renamed"})
        await browser.evaluate("window.dispatchEvent(new Event('focus'))")
        await browser.wait(shared + ".textContent.includes('Example Beta renamed')")
        state["phase"] = "released"
        await browser.evaluate("window.dispatchEvent(new Event('focus'))")
        await browser.wait(shared + ".textContent.includes('Available now')")
        assert not await browser.evaluate(shared + ".querySelector('.slot-holder')")
        await browser.shot(output, "selected-alpha-refreshed")
        await browser.click("Manage in Platform settings", "a")
        await browser.wait("document.activeElement.id === 'platform-service-slots'")
        assert await browser.evaluate("(() => { const r = document.activeElement.getBoundingClientRect(); return r.top >= 56 && r.bottom < innerHeight; })()")
        await browser.shot(output, "slot-management-destination")
        await browser.click("back to the fleet")
        await browser.click("Example Beta renamed", '[role="tab"]')
        await browser.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
        await browser.wait("parseFloat(getComputedStyle(document.querySelector('main')).paddingLeft) === 0")
        assert not await browser.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert not await browser.evaluate(region + ".textContent.includes('Alpha service')")
        assert await browser.evaluate(region + ".textContent.includes('Beta only')")
        await browser.shot(output, "selected-beta-phone")
        await browser.click("Manage in Platform settings", "a")
        await browser.wait("document.activeElement.id === 'platform-service-slots'")
        assert await browser.evaluate("(() => { const r = document.activeElement.getBoundingClientRect(); return r.top >= 56 && r.bottom < innerHeight; })()")
        await browser.click("back to the fleet")
        await browser.click("General", '[role="tab"]')
        await browser.wait(region + ".textContent.includes('No service slots held by or assigned to General.')")
        await browser.shot(output, "unassigned-project-phone")
        state["phase"] = "empty"
        await browser.evaluate("window.dispatchEvent(new Event('focus'))")
        await browser.wait(region + ".textContent.includes('No service slots configured.')")
        await browser.click_element("document.querySelector('.v-tabs .v-slide-group__prev')")
        await browser.wait("[...document.querySelectorAll('[role=tab]')].some(e => e.textContent.trim() === 'All projects' && e.getBoundingClientRect().left >= 0)")
        await browser.click("All projects", '[role="tab"]')
        assert await browser.evaluate(region + ".textContent.includes('No service slots configured.')")
        await browser.shot(output, "no-slots-phone")
        assert not browser.errors, browser.errors
    (output / "browser-results.json").write_text(json.dumps({
        "result": "pass", "viewports": ["1440x1000", "390x844"], "runtime_errors": browser.errors,
        "checks": ["All-project held slots with project names", "project eligibility and retained holdings", "foreign/overlap holders never available",
                   "HTTP refresh updates project names and availability", "read-only fleet rows", "management link focuses existing section on desktop/phone",
                   "empty project and empty slot configuration", "no phone overflow"],
        "limits": "Real Chromium and isolated HTTP/project stores; synthetic slot occupancy/service state, no live service or assignment changes.",
    }, indent=2) + "\n")


if __name__ == "__main__":
    fixture.main(probe, prepare=prepare, state_builder=inventory)
