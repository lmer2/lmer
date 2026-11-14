"""Issue 652's Platform settings layout against the built UI and real HTTP routes.

Usage: python web/tests/browser/platform_settings.py CHROMIUM OUTPUT
"""
import asyncio
import json
import urllib.request

from websockets.asyncio.client import connect

import projects as fixture
from lmer_platform import config, runs

SECTIONS = ["Fleet", "Assistants", "Chat and display", "Graph", "Service slots", "Projects", "Inbound chat"]
PANELS = dict(zip(SECTIONS, ["fleet", "assistants", "chat", "graph", "slots", "projects", "inbound"]))

VISIBLE_PANEL = "[...document.querySelectorAll('[data-panel]')].filter(e => e.checkVisibility()).map(e => e.dataset.panel)"
OVERFLOW = "document.documentElement.scrollWidth > innerWidth"
LEAVE = "document.querySelector('.settings-leave')?.checkVisibility()"

# Each button's ink over what it actually paints on: a tonal button's ground is
# its own ink at the underlay's opacity over the card, a flat one's is its fill.
# An ink with alpha is composited over that ground, as the browser draws it.
INKS = """((selector) => {
  const rgba = (v) => { const n = v.match(/[\\d.]+/g).map(Number); const s = v.startsWith('color(srgb') ? 255 : 1;
                        return [n[0] * s, n[1] * s, n[2] * s, n.length > 3 ? n[3] : 1]; };
  const over = (top, alpha, base) => top.slice(0, 3).map((c, i) => alpha * c + (1 - alpha) * base[i]);
  const ground = (e) => { for (let n = e.parentElement; n; n = n.parentElement) {
    const c = rgba(getComputedStyle(n).backgroundColor); if (c[3] > 0) return c; } return [255, 255, 255, 1]; };
  return [...document.querySelectorAll(selector)]
    .filter(e => e.checkVisibility() && !e.disabled && (e.closest('.v-btn') === e || e.classList.contains('v-chip')))
    .map(e => {
      const style = getComputedStyle(e); const ink = rgba(style.color); const fill = rgba(style.backgroundColor);
      const underlay = e.querySelector('.v-btn__underlay, .v-chip__underlay');
      const tonal = e.classList.contains('v-btn--variant-tonal') || e.classList.contains('v-chip--variant-tonal');
      const base = fill[3] > 0 ? over(fill, fill[3], ground(e)) : ground(e).slice(0, 3);
      const paint = tonal ? over(ink, ink[3] * Number(getComputedStyle(underlay).opacity), base) : base;
      return {label: (e.getAttribute('aria-label') || e.textContent.trim()).slice(0, 40),
              ink: `rgb(${over(ink, ink[3], paint).map(Math.round)})`, ground: `rgb(${paint.map(Math.round)})`};
    });
})"""
SETTINGS = ".platform-settings button, .settings-leave button, .platform-settings .settings-unsaved"


async def measure(browser, selector, prefix, measured):
    for row in await browser.evaluate(INKS + f"({json.dumps(selector)})"):
        row["ratio"] = round(fixture.contrast(row["ink"], row["ground"]), 2)
        measured[f"{prefix}: {row['label']}"] = row


# The measurement itself: a half-transparent black ink on white is about 3.95:1, not 21:1.
async def ink_check(browser):
    await browser.evaluate("""(() => { const e = document.createElement('button'); e.id = 'ink-check'; e.className = 'v-btn';
      e.textContent = 'x'; e.style.cssText = 'color: rgba(0, 0, 0, .5); background: #fff'; document.body.append(e); })()""")
    measured = {}
    await measure(browser, "#ink-check", "check", measured)
    await browser.evaluate("document.querySelector('#ink-check').remove()")
    ratio = measured["check: x"]["ratio"]
    assert 3.8 < ratio < 4.1, measured
    return ratio


def prepare(alpha, beta, settings):
    config.update_stored({"fleet_groups": [{"name": "Team", "patterns": ["team/"]}]})


# One free slot, so the slot editor opens on an existing row with its Remove button.
def inventory(_settings):
    rows = [{**run.to_dict(), "label": run.slug, "state": "waiting_on_you", "live": False,
             "attention": {"reason": "question", "note": "Question one"}} for run in runs.list_tracked()]
    slot = {"name": "Shared service", "state": "free", "applicable_projects": [], "needs_project_assignment": False,
            "occupants": [], "service_occupants": [], "etag": "e1", "preset": "service"}
    return {"runs": rows, "attention": rows, "sessions": [], "slots": [slot], "relations": [], "totals": {"runs": len(rows), "attention": len(rows)}}


async def scheme(browser, name):
    current = await browser.evaluate("[...document.querySelectorAll('header button')].find(e => (e.getAttribute('aria-label') || '').startsWith('colour scheme'))?.getAttribute('aria-label')")
    await browser.click(current)
    await browser.click(name, ".v-list-item")
    await browser.wait(f"document.querySelector('.v-application.v-theme--{name}')")


async def key(browser, name, code, vk, shift=False):
    modifiers = 8 if shift else 0
    await browser.call("Input.dispatchKeyEvent", type="keyDown", key=name, code=code, windowsVirtualKeyCode=vk, modifiers=modifiers)
    await browser.call("Input.dispatchKeyEvent", type="keyUp", key=name, code=code, windowsVirtualKeyCode=vk, modifiers=modifiers)


async def focused(browser):
    return await browser.evaluate("""(() => { const e = document.activeElement;
      const field = e.closest('.v-input');
      const label = e.labels?.[0]?.textContent.trim() || field?.querySelector('label')?.textContent.trim();
      return label || e.getAttribute('aria-label') || e.textContent.trim(); })()""")


async def choose(browser, title, mobile):
    assert await browser.evaluate("document.querySelector('.settings-picker').checkVisibility()") == mobile
    await browser.settings_section(title)


async def open_settings(browser):
    await browser.wait(browser.labelled("Platform settings"))
    await browser.click("Platform settings")
    await browser.wait("document.querySelector('.platform-settings #worker-capacity')")
    await asyncio.sleep(.5)


async def layout(browser, output, width, height, mobile):
    await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=mobile)
    await browser.reload()
    await browser.wait("document.querySelector('header [aria-label=\"Platform settings\"]')")
    await open_settings(browser)
    result = {"nav": await browser.evaluate("document.querySelector('.settings-nav').checkVisibility()"),
              "picker": await browser.evaluate("document.querySelector('.settings-picker').checkVisibility()")}
    assert result["nav"] != mobile and result["picker"] == mobile, result

    # Every section is one choice from the top, with nothing scrolling sideways.
    reached = {}
    for title in SECTIONS:
        await choose(browser, title, mobile)
        await browser.wait(f"JSON.stringify({VISIBLE_PANEL}) === {json.dumps(json.dumps([PANELS[title]]))}")
        assert not await browser.evaluate(OVERFLOW), (width, title)
        reached[title] = await browser.evaluate(f"[...document.querySelector('[data-panel=\"{PANELS[title]}\"]').querySelectorAll('h2')].map(e => e.textContent.trim())")
        await browser.shot(output, f"settings-{width}-{PANELS[title]}")
    result["sections"] = reached

    # The tint switch is in Chat and display, on the page rather than in a dialog.
    await choose(browser, "Chat and display", mobile)
    await browser.wait("document.querySelector('[data-panel=\"chat\"] .tint-operator')?.checkVisibility()")
    result["tint_visible"] = True

    # Keyboard order follows reading order: nav (or picker), then the card's fields.
    await choose(browser, "Fleet", mobile)
    await browser.wait(f"{VISIBLE_PANEL}[0] === 'fleet'")
    start = ".settings-picker input" if mobile else ".settings-nav button"
    await browser.evaluate(f"document.querySelector('{start}').focus()")
    order = [await focused(browser)]
    for _ in range(3 if mobile else 9):
        await key(browser, "Tab", "Tab", 9)
        order.append(await focused(browser))
    expected = (["Section"] if mobile else SECTIONS) + ["Maximum live workers across all projects", "Maximum live assistants"]
    assert order[:len(expected)] == expected, order
    result["tab_order"] = order
    return result


async def focus_ring(browser):
    await browser.evaluate("document.querySelector('.settings-nav button').focus()")
    await key(browser, "Tab", "Tab", 9)
    await key(browser, "Tab", "Tab", 9, shift=True)
    ring = await browser.evaluate("""(() => { const s = getComputedStyle(document.activeElement);
      return {outline: s.outlineStyle, width: s.outlineWidth, color: s.outlineColor, offset: s.outlineOffset,
              primary: `rgb(${getComputedStyle(document.querySelector('.v-application')).getPropertyValue('--v-theme-primary').trim()})`,
              surface: getComputedStyle(document.activeElement.closest('.v-main') || document.body).backgroundColor}; })()""")
    assert ring["outline"] == "solid" and ring["width"] == "2px", ring
    ring["contrast_on_page"] = fixture.contrast(ring["color"], ring["surface"])
    assert ring["contrast_on_page"] >= 3, ring
    return ring


# A header button's ring sits on the app bar, which a selected project paints in its colour.
async def bar_ring(browser):
    await browser.choose_project(browser.project)
    await browser.evaluate("document.querySelector('header [aria-label=\"Platform settings\"]').focus()")
    await key(browser, "Tab", "Tab", 9)
    await key(browser, "Tab", "Tab", 9, shift=True)
    ring = await browser.evaluate("""(() => { const e = document.activeElement; const s = getComputedStyle(e);
      return {label: e.getAttribute('aria-label'), outline: s.outlineStyle, color: s.outlineColor,
              bar: getComputedStyle(e.closest('header')).backgroundColor}; })()""")
    assert ring["label"] == "Platform settings" and ring["outline"] == "solid", ring
    ring["contrast_on_bar"] = fixture.contrast(ring["color"], ring["bar"])
    assert ring["contrast_on_bar"] >= 3, ring
    return ring


async def contrasts(browser, output, name):
    await scheme(browser, name)
    await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
    measured = {}
    for title in ("Fleet", "Graph", "Assistants", "Chat and display", "Service slots", "Inbound chat", "Projects"):
        await browser.click(title)
        await browser.wait(f"{VISIBLE_PANEL}[0] === {json.dumps(PANELS[title])}")
        if title == "Fleet":
            await browser.click_element("document.querySelector('#worker-capacity')")
            await browser.evaluate("(() => { const e = document.querySelector('#worker-capacity'); e.value = '9'; e.dispatchEvent(new Event('input', {bubbles: true})); })()")
            await browser.wait("!document.querySelector('[data-panel=\"fleet\"] .settings-save').disabled")
        await asyncio.sleep(.3)
        await measure(browser, SETTINGS, title, measured)
        if title == "Fleet":
            await browser.click("Graph")
            await browser.wait(LEAVE)
            await asyncio.sleep(.3)
            await measure(browser, ".settings-leave button", "leave dialog", measured)
            await browser.shot(output, f"leave-dialog-{name}")
            await browser.click("Discard")
            await browser.wait(f"!{LEAVE}")
        if title == "Service slots":
            await browser.click_element("document.querySelector('[data-panel=\"slots\"] button[title=\"edit this slot\"]')")
            await browser.wait("document.querySelector('.slot-editor')?.checkVisibility()")
            await asyncio.sleep(.3)
            await measure(browser, ".slot-editor button", "slot editor", measured)
            await browser.shot(output, f"slot-editor-{name}")
            await browser.click("cancel", ".slot-editor button")
            await browser.wait("!document.querySelector('.slot-editor')?.checkVisibility()")
    await browser.click("Fleet")
    await browser.shot(output, f"settings-1440-fleet-{name}")

    # Project settings shares the button hierarchy: Save, Close, Custom…, Archive and its confirm.
    await browser.choose_project(browser.project)
    await browser.switcher_entry(f"Project settings for {browser.project}")
    await browser.wait("document.querySelector('.project-settings .custom-color')")
    await asyncio.sleep(.3)
    await measure(browser, ".project-settings button", "Project settings", measured)
    await browser.click("Archive project")
    await browser.wait("document.querySelector('.archive-confirm')?.checkVisibility()")
    await asyncio.sleep(.3)
    await measure(browser, ".archive-confirm button", "archive confirm", measured)
    await browser.shot(output, f"archive-confirm-{name}")
    await browser.click("Cancel")
    await browser.wait("!document.querySelector('.archive-confirm')?.checkVisibility()")
    focus = await browser.evaluate("document.activeElement.textContent.trim()")
    assert focus == "Archive project", focus
    await browser.click("Close")
    await open_settings(browser)

    failing = {label: row for label, row in measured.items() if row["ratio"] < 4.5}
    assert not failing, failing
    return measured


async def leaving(browser):
    await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
    await browser.reload()
    await browser.wait("document.querySelector('header [aria-label=\"Platform settings\"]')")
    await open_settings(browser)
    await browser.evaluate("(() => { const e = document.querySelector('#worker-capacity'); e.value = '11'; e.dispatchEvent(new Event('input', {bubbles: true})); })()")
    await browser.wait("document.querySelector('[data-panel=\"fleet\"] .settings-unsaved')")
    dot = await browser.evaluate("!!document.querySelector('.settings-nav [data-section=\"fleet\"] .settings-dirty-dot')")
    await browser.click("back to the fleet")
    await browser.wait(LEAVE)
    asked = await browser.evaluate("document.querySelector('.settings-leave').textContent")
    await browser.click("Keep editing")
    await browser.wait(f"!{LEAVE}")
    kept = await browser.evaluate("document.querySelector('#worker-capacity').value")
    await browser.click("back to the fleet")
    await browser.wait(LEAVE)
    await browser.click("Discard")
    await browser.wait("!document.querySelector('.platform-settings')")
    stored = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{browser.url}api/settings", headers={"Authorization": f"Bearer {browser.secret}"}), timeout=5).read())
    result = {"dot": dot, "asked": asked, "kept": kept, "stored_workers": stored["max_concurrent_sessions"]}
    assert dot and "Fleet has unsaved changes." in asked and kept == "11" and stored["max_concurrent_sessions"] != 11, result
    return result


async def probe(chrome, output, url, secret, alpha, beta, settings):
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{chrome}/json/list", timeout=5))
    page = next(target for target in targets if target["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as connection:
        browser = fixture.Browser(connection)
        browser.output, browser.url, browser.secret, browser.project = output, url, secret, alpha["name"]
        await browser.call("Runtime.enable")
        await browser.call("Network.enable")
        await browser.call("Network.setExtraHTTPHeaders", headers={"Authorization": f"Bearer {secret}"})
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.call("Page.navigate", url=url)
        await browser.wait("document.querySelector('header [aria-label=\"Platform settings\"]')")
        report = {"desktop": await layout(browser, output, 1440, 1000, False)}
        report["focus_ring"] = await focus_ring(browser)
        report["bar_ring"] = await bar_ring(browser)
        report["phone"] = await layout(browser, output, 360, 800, True)
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.reload()
        await browser.wait("document.querySelector('header [aria-label=\"Platform settings\"]')")
        await open_settings(browser)
        report["ink_check"] = await ink_check(browser)
        report["contrast"] = {name: await contrasts(browser, output, name) for name in ("light", "dark")}
        report["leaving"] = await leaving(browser)
        assert not browser.errors, browser.errors
        (output / "browser-results.json").write_text(json.dumps({"result": "pass", **report}, indent=2) + "\n")


if __name__ == "__main__":
    fixture.main(probe, prepare=prepare, state_builder=inventory)
