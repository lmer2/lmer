"""Eight project UI controls against isolated stores/HTTP and neutral fixture runs.

Usage: python this_file.py CHROMIUM OUTPUT [before]
The optional before mode captures the previous built UI without new assertions.
"""
import asyncio
import json
import sys
import urllib.request

from websockets.asyncio.client import connect

import projects as fixture
from project_navigation import key
from lmer_platform import assistant, config, projects, registry, runs
from lmer_platform import project_context as scope


def prepare(alpha, beta, settings):
    config.update_stored({"max_live_assistants": 2})
    projects.reconcile(settings)
    for project, name, color in ((alpha, "Example Alpha", "Blue"), (beta, "Example Beta", "Teal")):
        projects.update(project["id"], {"name": name, "color": color})
        project.update(name=name, color=color)
        with scope.bound(project["id"]):
            assistant.set_instructions(f"Standing instructions for {name}: preserve the working brief.")
            for suffix in ("complete", "held", "active"):
                runs.track("forge.test", "shared/repository", f"{name.split()[-1].lower()}-{suffix}")
    with scope.bound(beta["id"]):
        runs.forget("forge.test", "shared/repository", "beta-question")
    attention = projects.create("Example Attention")
    projects.update(attention["id"], {"color": "Orange"})
    projects.stop(attention["id"])
    with scope.bound(attention["id"]):
        runs.track("forge.test", "shared/repository", "attention-question")
    archived = projects.create("Example Archive")
    projects.update(archived["id"], {"color": "Purple"})
    projects.stop(archived["id"])
    projects.archive(archived["id"], {"runs": []})


def inventory(_settings):
    rows = []
    for run in runs.list_tracked():
        state = "waiting_on_you" if run.slug.endswith("question") else run.slug.rsplit("-", 1)[-1]
        rows.append({**run.to_dict(), "label": run.slug, "state": state, "live": state == "active",
                     "attention": {"reason": "question"} if state == "waiting_on_you" else None})
    for project in projects.listing():
        status = projects.status(project["id"])
        if not status["running"]:
            continue
        rows.append({"host": "forge.test", "project": "shared/repository", "slug": "orchestrate-fleet",
                     "label": "orchestrate-fleet", "orchestrator": True, "state": "running", "live": True,
                     "session": registry.read_session(status["session_id"]), "attention": None})
    attention = [row for row in rows if row["attention"]]
    return {"runs": rows, "attention": attention, "sessions": [], "slots": [], "relations": [],
            "totals": {"runs": len(rows), "attention": len(attention), "live": sum(row["live"] for row in rows)}}


async def input_text(browser, expression, text):
    await browser.click_element(expression)
    await browser.evaluate(expression + ".select()")
    await browser.call("Input.insertText", text=text)


# Move focus off the bar onto <main>. A bare blur() will not do: Vuetify keeps a
# focus tooltip open when focus parks on the body (blur with no relatedTarget).
PARK_FOCUS = "(() => { const main = document.querySelector('main'); main.tabIndex = -1; main.focus(); })()"


async def tooltips(browser):
    buttons = "[...document.querySelectorAll('header button')].filter(e => e.checkVisibility())"
    labels = await browser.evaluate(buttons + ".map(e => e.getAttribute('aria-label'))")
    assert all(labels), labels
    for index, label in enumerate(labels):
        target = buttons + f"[{index}]"
        position = await browser.evaluate("(() => { const r = " + target + ".getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()")
        assert 0 <= position["x"] <= await browser.evaluate("innerWidth"), (label, position)
        await browser.evaluate(PARK_FOCUS)
        await browser.call("Input.dispatchMouseEvent", type="mouseMoved", **position)
        shown = "(() => { const b = " + target + "; const tip = document.getElementById(b.getAttribute('aria-describedby')); return tip?.classList.contains('v-overlay--active') && tip.textContent.trim() === " + json.dumps(label) + "; })()"
        await browser.wait(shown)
        await browser.call("Input.dispatchMouseEvent", type="mouseMoved", x=1, y=800)
        await browser.wait("!document.getElementById(" + target + ".getAttribute('aria-describedby'))?.classList.contains('v-overlay--active')")
        await asyncio.sleep(.1)
        await key(browser, "Tab", "Tab", 9, "")
        await browser.evaluate(target + ".focus()")
        await browser.wait(shown)
    await browser.evaluate(PARK_FOCUS)
    return labels


async def settings_screens(browser, output, before, width, project_id):
    await browser.switcher_entry("Project settings for Example Beta")
    await browser.wait("document.querySelector('main').textContent.includes('Standing instructions')")
    if not before:
        await browser.wait("document.querySelector('.v-expansion-panel-title')")
        assert await browser.evaluate("document.querySelector('.v-expansion-panel-title').getAttribute('aria-expanded')") == "false"
        assert not await browser.evaluate("document.querySelector('.project-orders textarea')?.checkVisibility()")
    await browser.shot(output, f"project-settings-collapsed-{width}")
    if not before:
        await browser.click_element("document.querySelector('.v-expansion-panel-title')")
        await browser.wait("document.querySelector('.project-orders textarea')?.checkVisibility()")
        orders = "document.querySelector('.project-orders textarea')"
        assert "Example Beta" in await browser.evaluate(orders + ".value")
        text = "Example Beta edited standing instructions: preserve this draft through collapse."
        await input_text(browser, orders, text)
        await browser.click_element("document.querySelector('.v-expansion-panel-title')")
        await browser.wait("document.querySelector('.v-expansion-panel-title').getAttribute('aria-expanded') === 'false'")
        await browser.click("Save project")
        await browser.wait("!document.querySelector('.v-btn--loading')")
        error = await browser.evaluate("document.querySelector('.project-settings .v-alert')?.textContent || ''")
        assert not error, error
        with scope.bound(project_id):
            stored = assistant.read_state().instructions
        assert stored == text, stored
        await browser.click_element("document.querySelector('.v-expansion-panel-title')")
        await browser.wait(orders + "?.checkVisibility()")
        assert await browser.evaluate(orders + ".value") == text
        await browser.evaluate(orders + ".scrollIntoView({block:'center'})")
        await browser.wait(orders + ".getBoundingClientRect().bottom < innerHeight")
        await asyncio.sleep(.3)
        await browser.shot(output, f"project-settings-expanded-{width}")
        await browser.click("Close")
        await browser.switcher_entry("Project settings for Example Beta")
        await browser.wait("document.querySelector('.v-expansion-panel-title')?.getAttribute('aria-expanded') === 'false'")
    await browser.click("Platform settings")
    await browser.wait("document.querySelector('h1')?.textContent === 'Platform settings'")
    if not before:
        fields = await browser.evaluate("[...document.querySelectorAll('.capacity-limits input')].map(e => ({type:e.type,step:e.step,min:e.min,value:e.value,x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,width:e.getBoundingClientRect().width}))")
        assert len(fields) == 2 and all(row["type"] == "number" and row["step"] == "1" and row["min"] == "1" for row in fields), fields
        assert abs(fields[0]["y"] - fields[1]["y"]) < 2 and fields[0]["x"] < fields[1]["x"], fields
        assert all(row["width"] >= 60 for row in fields), fields
        await input_text(browser, "document.querySelector('#worker-capacity')", "10")
        await input_text(browser, "document.querySelector('#assistant-capacity')", "3")
        await browser.click("Save limit")
        await browser.wait("!document.querySelector('.v-btn--loading')")
        assert config.load().max_concurrent_sessions == 10 and config.load().max_live_assistants == 3
    await browser.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(.2)
    await browser.shot(output, f"platform-capacity-{width}")
    archive = "[...document.querySelectorAll('.v-list-item')].find(e => e.textContent.includes('Example Archive'))"
    await browser.evaluate(archive + ".scrollIntoView({block:'center'})")
    await asyncio.sleep(.3)
    if not before:
        assert await browser.evaluate("(() => { const row = " + archive + "; return getComputedStyle(row.querySelector('.v-chip')).backgroundColor === getComputedStyle(row.querySelector('button')).backgroundColor && getComputedStyle(row).borderInlineStartColor === getComputedStyle(row.querySelector('button')).backgroundColor; })()")
    await browser.shot(output, f"archived-project-{width}")
    assert not await browser.evaluate("document.documentElement.scrollWidth > innerWidth"), width
    await browser.click("back to the fleet")


async def probe(chrome, output, url, secret, alpha, beta, settings):
    before = len(sys.argv) > 3 and sys.argv[3] == "before"
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{chrome}/json/list"))
    page = next(target for target in targets if target["type"] == "page")
    checked = []
    async with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as connection:
        browser = fixture.Browser(connection)
        browser.output = output
        await browser.call("Runtime.enable")
        await browser.call("Network.enable")
        await browser.call("Network.setExtraHTTPHeaders", headers={"Authorization": f"Bearer {secret}"})
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.call("Page.navigate", url=url)
        await browser.wait("document.querySelectorAll('.project-nav').length === 4")
        await browser.choose_project("Example Beta")
        for name in ("Example Alpha", "Example Beta"):
            group = "document.querySelector('.project-nav[aria-label=" + json.dumps(name) + "]')"
            await browser.click_element("[..." + group + ".querySelectorAll('.run-row')].find(e => e.textContent.includes('orchestrate-fleet'))")
            await browser.wait(browser.SWITCHER + "?.textContent.trim() === " + json.dumps(name))
            if not before:
                assert await browser.evaluate("document.querySelectorAll('.project-nav [aria-current=true]').length") == 1
                assert await browser.evaluate(group + ".querySelector('[aria-current=true]') !== null")
                assert not await browser.evaluate("document.querySelector('.run-project-identity')")
            await browser.shot(output, "selected-" + name.split()[-1].lower() + "-desktop")
        if not before:
            checked += await tooltips(browser)
            for name, dividers in (("Example Alpha", 1), ("Example Beta", 0), ("Example Attention", 0)):
                group = "document.querySelector('.project-nav[aria-label=" + json.dumps(name) + "]')"
                assert await browser.evaluate(group + ".querySelectorAll('details,summary').length") == 0
                assert await browser.evaluate(group + ".querySelectorAll('.section-title').length") == 0
                assert await browser.evaluate(group + ".querySelectorAll('.run-attention-divider').length") == dividers
            with scope.bound(beta["id"]):
                assert "edited" not in assistant.read_state().instructions
        await browser.click("back to the fleet")
        if not before:
            checked += await tooltips(browser)
        await browser.choose_project("All projects")
        await browser.wait("document.querySelectorAll('main section').length === 4")
        await browser.shot(output, "all-projects-desktop")
        if not before:
            await browser.click("Forget all completed")
            await browser.wait("document.querySelector('.v-dialog')?.textContent.includes('Forget 2 completed runs?')")
            await browser.shot(output, "all-projects-forget-confirmation")
            await browser.click("cancel")
            assert len([row for row in runs.list_tracked() if row.slug.endswith('complete')]) == 2
            await browser.click("Forget all completed")
            await browser.click("Forget 2 runs")
            await browser.wait("!document.querySelector('main').textContent.includes('alpha-complete') && !document.querySelector('main').textContent.includes('beta-complete')")
            remaining = [row.slug for row in runs.list_tracked()]
            assert all(name in remaining for name in ("alpha-held", "beta-held", "alpha-active", "beta-active")), remaining
            assert not any(name.endswith('complete') for name in remaining), remaining
        await browser.choose_project("Example Beta")
        await settings_screens(browser, output, before, 1440, beta["id"])
        for width in (390, 320):
            await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=844, deviceScaleFactor=1, mobile=True)
            await browser.wait("document.querySelector('[aria-label=\"show the run list\"]')")
            sidebar = "document.querySelector('.v-navigation-drawer--left')"
            if await browser.evaluate(sidebar + ".classList.contains('v-navigation-drawer--active')"):
                await browser.click("show the run list")
            await browser.wait(sidebar + ".getBoundingClientRect().right <= 0")
            if not before:
                checked += await tooltips(browser)
            await browser.click("show the run list")
            await browser.wait(sidebar + ".getBoundingClientRect().left >= 0")
            await browser.shot(output, f"sidebar-phone-{width}")
            await browser.click("show the run list")
            await browser.wait(sidebar + ".getBoundingClientRect().right <= 0")
            await settings_screens(browser, output, before, width, beta["id"])
            assert not await browser.evaluate("document.documentElement.scrollWidth > innerWidth")
        if not before:
            with scope.bound(beta["id"]):
                assert assistant.read_state().instructions == "Example Beta edited standing instructions: preserve this draft through collapse."
            with scope.bound(alpha["id"]):
                assert assistant.read_state().instructions == "Standing instructions for Example Alpha: preserve the working brief."
        assert not browser.errors, browser.errors
    (output / "browser-results.json").write_text(json.dumps({
        "result": "before capture" if before else "pass", "viewports": ["1440x1000", "390x844", "320x844"],
        "global_actions_hover_and_focus": sorted(set(checked)), "runtime_errors": browser.errors,
        "checks": [] if before else ["unique project assistant selection", "selected detail badge deduplicated; All projects names retained",
                                     "mixed/attention-only/ordinary-only rows without nested collapse", "confirmed All-project forgetting excludes active/held",
                                     "collapsed editable orders survive save/reopen and stay project-bound", "capacity number fields share a row and save",
                                     "archived identity/restore color", "desktop/phone header tooltips and no overflow"],
        "limits": "Real browser and temporary HTTP/stores; synthetic inventory states and sleeping assistants. No live platform or external transports.",
    }, indent=2) + "\n")


if __name__ == "__main__":
    fixture.main(probe, prepare=prepare, state_builder=inventory)
