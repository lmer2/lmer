"""Isolated Chromium acceptance probe. Usage: python web/tests/browser/projects.py CHROMIUM OUTPUT.

Uses the built UI, actual platform HTTP routes/stores, and sleeping assistant children.
No external transport, container runtime, or live host daemon is used.
"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

import uvicorn
from websockets.asyncio.client import connect

from lmer_platform import api, ask, assistant, config, projects, registry, runs, spawn, store
from lmer_platform import project_context as scope


# Issue 575: the pane reaches the bottom of the window at every size, its toolbar
# stays one compact row, and the question badge's corner is reserved in the field.
FILL = """(() => {
  const pane = [...document.querySelectorAll('.uber-pane')].find(e => e.checkVisibility());
  const card = pane.querySelector('.chat-fill');
  const slot = card.querySelector('.v-field__append-inner .question-slot');
  return {inner: innerHeight, pane: pane.getBoundingClientRect().bottom, card: card.getBoundingClientRect().bottom,
          scroller: card.querySelector('.chat').getBoundingClientRect().height,
          toolbar: pane.querySelector('.uber-toolbar').getBoundingClientRect().height,
          slot: slot && slot.getBoundingClientRect().width, badge: !!slot?.querySelector('.open-questions'),
          overflow: document.documentElement.scrollWidth > innerWidth};
})()"""


TOOLBAR = """(() => {
  const toolbar = [...document.querySelectorAll('.uber-toolbar')].find(e => e.checkVisibility());
  return {background: getComputedStyle(toolbar).backgroundColor,
          ink: getComputedStyle(toolbar.querySelector('[aria-label="project settings"]')).color,
          bar: getComputedStyle(document.querySelector('header.v-app-bar')).backgroundColor};
})()"""

IDENTITY = "[...document.querySelectorAll('.uber-identity')].find(e => e.checkVisibility())"
IDENTITY_SHOWN = ("[...document.querySelectorAll('.v-tooltip .v-overlay__content')].some(e => e.checkVisibility() && "
                  f"e.textContent.trim() === {IDENTITY}.getAttribute('aria-label'))")


def luminance(value):
    rgb = [int(number) / 255 for number in re.findall(r"\d+", value)[:3]]
    rgb = [part / 12.92 if part <= .04045 else ((part + .055) / 1.055) ** 2.4 for part in rgb]
    return sum(part * weight for part, weight in zip(rgb, (.2126, .7152, .0722)))


def contrast(first, second):
    first, second = luminance(first), luminance(second)
    return (max(first, second) + .05) / (min(first, second) + .05)


class Browser:
    def __init__(self, connection):
        self.connection, self.serial, self.errors = connection, 0, []

    async def call(self, method, **params):
        self.serial += 1
        identity = self.serial
        await self.connection.send(json.dumps({"id": identity, "method": method, "params": params}))
        while True:
            reply = json.loads(await asyncio.wait_for(self.connection.recv(), 20))
            if reply.get("method") == "Runtime.exceptionThrown":
                self.errors.append(reply["params"])
            if reply.get("id") == identity:
                assert "error" not in reply, reply
                return reply.get("result", {})

    async def evaluate(self, expression):
        result = await self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in result, result
        return result.get("result", {}).get("value")

    async def wait(self, expression):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if await self.evaluate(f"Boolean({expression})"):
                return
            await asyncio.sleep(.1)
        await self.shot(self.output, "failure")
        (self.output / "failure.html").write_text(await self.evaluate("document.body.innerHTML"))
        raise AssertionError(f"Browser condition timed out: {expression}")

    async def centre(self, expression):
        return await self.evaluate("(() => { const e = " + expression + "; const r = e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()")

    async def click_element(self, expression):
        await self.evaluate(expression + ".scrollIntoView({block:'center'})")
        await asyncio.sleep(.5)
        position = await self.centre(expression)
        await self.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, **position)
        await self.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, **position)
        await asyncio.sleep(.2)

    async def tap_element(self, expression):
        """A touchscreen tap; Chromium derives the compatibility mouse events and the click."""
        position = await self.centre(expression)
        await self.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[position])
        await self.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        await asyncio.sleep(.2)

    @staticmethod
    def labelled(label, selector="button"):
        return f"[...document.querySelectorAll({json.dumps(selector)})].find(e => e.checkVisibility() && !e.closest('[inert]') && e.getBoundingClientRect().right > 0 && e.getBoundingClientRect().left < innerWidth && (e.getAttribute('aria-label') || e.textContent.trim()) === {json.dumps(label)})"

    async def click(self, label, selector="button"):
        await self.click_element(self.labelled(label, selector))

    # The app bar's project switcher: on desktop in the bar itself, on phones in
    # the bar's extension row. Only one is rendered, but match the visible one.
    SWITCHER = "[...document.querySelectorAll('.project-switcher')].find(e => e.checkVisibility())"
    SWITCHER_LIST = "[...document.querySelectorAll('.project-switcher-list')].find(e => e.checkVisibility())"

    async def switcher_entry(self, title):
        """Open the switcher menu and pick the entry titled `title` with real pointer events."""
        await self.wait(self.SWITCHER)
        await self.click_element(self.SWITCHER)
        entry = f"[...{self.SWITCHER_LIST}.querySelectorAll('.v-list-item')].find(e => e.querySelector('.v-list-item-title')?.textContent.trim() === {json.dumps(title)})"
        await self.wait(f"{self.SWITCHER_LIST} && {entry}")
        await self.click_element(entry)
        await self.wait(f"!{self.SWITCHER_LIST}")

    async def choose_project(self, name):
        """Select a project (or "All projects") through the app bar's switcher."""
        await self.switcher_entry(name)
        await self.wait(f"{self.SWITCHER}?.textContent.trim() === {json.dumps(name)}")

    async def shot(self, output, name):
        reply = await self.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        (output / f"{name}.png").write_bytes(base64.b64decode(reply["data"]))


async def rotating_composer(browser, output, project, settings):
    await browser.evaluate("""(() => {
      const input = [...document.querySelectorAll('input[type=file]')].find(e => e.closest('.composer')?.checkVisibility());
      const files = new DataTransfer();
      files.items.add(new File(['rotation attachment bytes'], 'rotation.txt', {type: 'text/plain'}));
      input.files = files.files;
      input.dispatchEvent(new Event('change', {bubbles: true}));
    })()""")
    await browser.wait("[...document.querySelectorAll('.attachments')].some(e => e.checkVisibility() && e.textContent.includes('rotation.txt'))")
    entered, release = threading.Event(), threading.Event()
    original = assistant.start
    def held_start(*args, **kwargs):
        entered.set()
        assert release.wait(20), "Browser did not release the isolated rotation"
        return original(*args, **kwargs)
    with patch.object(assistant, 'start', held_start), ThreadPoolExecutor(max_workers=1) as pool:
        rotation = pool.submit(projects.rotate, project['id'], settings)
        try:
            assert await asyncio.to_thread(entered.wait, 20)
            during = projects.status(project['id'])
            assert not during['running'] and during['lifecycle'] == 'rotating' and during['history_session']
            await browser.wait("document.body.textContent.includes('Restarting Beta · Ratchet')")
            assert await browser.evaluate("[...document.querySelectorAll('.attachments')].some(e => e.checkVisibility() && e.textContent.includes('rotation.txt'))")
            assert await browser.evaluate("[...document.querySelectorAll('[aria-label=\"send to this session\"]')].filter(e => e.checkVisibility()).every(e => e.disabled)")
            await browser.shot(output, 'rotation-gap-attachment')
        finally:
            release.set()
        after = rotation.result(timeout=20)
    assert after['running'] and after['session_id'] != during['history_session']
    await browser.wait("[...document.querySelectorAll('.uber-pane[aria-label^=\"Beta\"] [aria-label=\"restart Ratchet\"]')].some(e => e.checkVisibility())")
    await browser.wait("[...document.querySelectorAll('textarea')].some(e => e.checkVisibility() && !e.disabled)")
    assert await browser.evaluate("[...document.querySelectorAll('.attachments')].some(e => e.checkVisibility() && e.textContent.includes('rotation.txt'))")
    assert await browser.evaluate("[...document.querySelectorAll('textarea')].some(e => e.checkVisibility() && e.value === 'Beta draft retained through admission')")
    await browser.shot(output, 'rotation-successor-attachment')


async def fills_the_window(browser, name):
    await browser.wait("[...document.querySelectorAll('.uber-pane .chat-fill')].some(e => e.checkVisibility())")
    await asyncio.sleep(.5)
    sizes = await browser.evaluate(FILL)
    # Both sides: short of the bottom is the old gap, past it is a transcript that
    # grows instead of scrolling and pushes the composer off the screen.
    assert abs(sizes["pane"] - sizes["inner"]) <= 1, (name, sizes)
    assert 0 <= sizes["inner"] - sizes["card"] <= 16, (name, sizes)
    assert sizes["toolbar"] <= 56 and sizes["slot"] == 36 and not sizes["overflow"], (name, sizes)
    return sizes


async def sidebar_chat(browser, name):
    """Open a project's chat from its sidebar button, opening the phone's run list first."""
    sidebar = "document.querySelector('.v-navigation-drawer--left')"
    if not await browser.evaluate(sidebar + ".classList.contains('v-navigation-drawer--active')"):
        await browser.click("show the run list")
    await browser.wait(sidebar + ".getBoundingClientRect().left >= 0")
    await browser.click_element(f"document.querySelector('.project-assistant[aria-label^={json.dumps('Chat with ' + name)}]')")


async def identity_stays_shown(browser, gesture):
    # Sampled after the tooltip's transitions, so a show-then-hide on one gesture is caught.
    seen = []
    for pause in (.05, .3, 1):
        await asyncio.sleep(pause)
        seen.append(await browser.evaluate(f"Boolean({IDENTITY_SHOWN})"))
    assert all(seen), (gesture, seen)


async def identity_on_touch(chrome, output, secret, url):
    """One real tap on the robot leaves its identity up, and a tap elsewhere puts it away.

    Touch input only, in a page of its own: a tap also fires the compatibility
    mouseenter that element.click() skips, and a page this probe has driven with
    a mouse hands hover back to that cursor after the tap, which a phone cannot.
    """
    request = urllib.request.Request(f"http://127.0.0.1:{chrome}/json/new?about:blank", method="PUT")
    page = json.load(urllib.request.urlopen(request))
    try:
        async with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as connection:
            phone = Browser(connection)
            phone.output = output
            await phone.call("Runtime.enable")
            await phone.call("Network.enable")
            await phone.call("Network.setExtraHTTPHeaders", headers={"Authorization": f"Bearer {secret}"})
            await phone.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
            await phone.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=1)
            await phone.call("Page.navigate", url=url)
            await phone.wait(phone.labelled("show the run list"))
            await phone.tap_element(phone.labelled("show the run list"))
            await phone.wait("document.querySelector('.v-navigation-drawer--left').getBoundingClientRect().left >= 0")
            await phone.tap_element("document.querySelector('.project-assistant[aria-label^=\"Chat with Beta\"]')")
            await phone.wait(f"{IDENTITY}?.getAttribute('aria-label').includes('generation')")
            await asyncio.sleep(.5)
            await phone.tap_element(IDENTITY)
            await identity_stays_shown(phone, "tap")
            await phone.shot(output, "robot-identity-tap")
            await phone.tap_element("[...document.querySelectorAll('.uber-pane .chat')].find(e => e.checkVisibility())")
            await phone.wait(f"!{IDENTITY_SHOWN}")
            assert not phone.errors, phone.errors
    finally:
        urllib.request.urlopen(f"http://127.0.0.1:{chrome}/json/close/{page['id']}").read()


async def identity_on_hover(browser):
    """A mouse hover shows the robot's identity, and a click on the robot keeps it up."""
    centre = await browser.centre(IDENTITY)
    await browser.call("Input.dispatchMouseEvent", type="mouseMoved", **centre)
    await identity_stays_shown(browser, "hover")
    await browser.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, **centre)
    await browser.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, **centre)
    await identity_stays_shown(browser, "hover then click")
    await browser.call("Input.dispatchMouseEvent", type="mouseMoved", x=1, y=centre["y"] + 300)
    await browser.wait(f"!{IDENTITY_SHOWN}")


async def probe(chrome, output, url, secret, alpha, beta, settings):
    deadline = time.monotonic() + 20
    page = None
    while page is None and time.monotonic() < deadline:
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{chrome}/json/list"))
        page = next((target for target in targets if target["type"] == "page"), None)
        if page is None:
            await asyncio.sleep(.1)
    assert page, "Chromium did not create a page"
    async with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as connection:
        browser = Browser(connection)
        browser.output = output
        await browser.call("Runtime.enable")
        await browser.call("Network.enable")
        await browser.call("Network.setExtraHTTPHeaders", headers={"Authorization": f"Bearer {secret}"})
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.call("Page.navigate", url=url)
        await browser.wait("document.querySelectorAll('main section').length === 3")
        groups = await browser.evaluate("[...document.querySelectorAll('main section')].map(e => e.textContent)")
        assert "alpha-question" in groups[0] and "beta-question" not in groups[0], groups
        assert "beta-question" in groups[1] and "alpha-question" not in groups[1], groups
        await browser.shot(output, "all-projects-desktop")
        await browser.click_element("document.querySelector('.project-assistant[aria-label^=\"Chat with Alpha\"]')")
        await browser.wait("document.querySelector('.uber-pane')?.getAttribute('aria-label').startsWith('Alpha')")
        await browser.choose_project("All projects")
        assert await browser.evaluate("[...document.querySelectorAll('.uber-pane')].some(e => e.checkVisibility() && e.getAttribute('aria-label').startsWith('Alpha'))")
        await browser.shot(output, "all-projects-bound-chat")
        await browser.evaluate("document.querySelector('[aria-label=\"close the Ratchet chat\"]').click()")
        await browser.choose_project("Beta")
        # The bar control's aria-label carries the mood reason after the name
        # ("talk to Ratchet · idle: …"), so match it by prefix.
        await browser.click_element("[...document.querySelectorAll('header button')].find(e => e.checkVisibility() && (e.getAttribute('aria-label') || '').startsWith('talk to Ratchet'))")
        await browser.wait("[...document.querySelectorAll('.uber-pane')].some(e => e.checkVisibility() && e.getAttribute('aria-label').startsWith('Beta'))")
        await browser.wait("document.body.textContent.includes('Waiting to start — assistant limit reached')")
        assert projects.status(beta["id"])["history_session"]
        await browser.click_element("[...document.querySelectorAll('textarea')].find(e => e.checkVisibility() && !e.disabled)")
        await browser.call("Input.insertText", text="Beta draft retained through admission")
        await browser.shot(output, "waiting-assistant")
        await browser.click("Cancel start")
        await browser.wait("document.body.textContent.includes('Stopped until you choose Start')")
        assert projects.get(beta["id"])["desired"] == "stopped"
        await browser.click("Start")
        await browser.wait("document.body.textContent.includes('Waiting to start — assistant limit reached')")
        projects.stop(alpha["id"])
        projects.reconcile(settings)
        await browser.wait("[...document.querySelectorAll('.uber-pane[aria-label^=\"Beta\"] [aria-label=\"restart Ratchet\"]')].some(e => e.checkVisibility())")
        await browser.wait("[...document.querySelectorAll('textarea')].some(e => e.checkVisibility() && !e.disabled)")
        assert await browser.evaluate("[...document.querySelectorAll('textarea')].filter(e => e.checkVisibility() && !e.disabled).some(e => e.value === 'Beta draft retained through admission')")
        await browser.shot(output, "historical-draft-admitted")
        await rotating_composer(browser, output, beta, settings)
        fill = {}
        with patch.object(ask, "open_question_count", lambda _session: 2):
            await browser.call("Emulation.setDeviceMetricsOverride", width=3840, height=2160, deviceScaleFactor=1, mobile=False)
            await browser.wait("[...document.querySelectorAll('.uber-pane .open-questions')].some(e => e.checkVisibility())")
            fill["3840x2160"] = await fills_the_window(browser, "4k")
            assert fill["3840x2160"]["badge"]
            await browser.shot(output, "uber-pane-4k")
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.wait("![...document.querySelectorAll('.uber-pane .open-questions')].some(e => e.checkVisibility())")
        cleared = await fills_the_window(browser, "badge cleared")
        await identity_on_hover(browser)
        assert cleared["slot"] == 36 and not cleared["badge"], cleared
        fill["1440x1000"] = cleared
        await browser.evaluate("[...document.querySelectorAll('[aria-label=\"close the Ratchet chat\"]')].find(e => e.checkVisibility()).click()")
        await browser.switcher_entry("Project settings for Beta")
        swatches = "[...document.querySelectorAll('.project-color .v-color-picker-swatches__color')]"
        await browser.wait(swatches + ".length === " + str(len(projects.COLORS)))
        bar = "document.querySelector('header.v-app-bar')"
        contrasts = {}
        # The swatches are the presets in palette order, so swatch i is COLORS[i].
        for index, color in enumerate(projects.COLORS):
            await browser.click_element(swatches + f"[{index}]")
            await browser.wait(f"document.querySelector('.project-color .v-chip')?.textContent.trim() === {json.dumps(color)}")
            await browser.click("Save project")
            await browser.wait(f"!document.querySelector('.v-btn--loading') && {browser.SWITCHER}?.textContent.trim() === 'Beta'")
            assert projects.get(beta["id"])["color"] == color
            await browser.wait(f"getComputedStyle({bar}).backgroundColor === getComputedStyle(document.querySelector('.project-color .v-chip')).backgroundColor")
            colors = await browser.evaluate("(() => { const style = getComputedStyle(" + bar + "); return [style.color, style.backgroundColor]; })()")
            contrasts[color] = contrast(*colors)
            assert contrasts[color] >= 4.5, (color, colors, contrasts[color])
        await browser.call("Page.reload")
        await browser.wait(f"{browser.SWITCHER}?.textContent.trim() === 'Beta'")
        assert projects.get(beta["id"])["color"] == "Gray"
        await browser.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
        await browser.wait("document.querySelector('[aria-label=\"show the run list\"]')")
        await sidebar_chat(browser, "Beta")
        await browser.wait("[...document.querySelectorAll('.uber-pane')].some(e => e.checkVisibility() && e.getAttribute('aria-label').startsWith('Beta'))")
        overflow = await browser.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert not overflow, "Page overflows the phone viewport"
        with patch.object(ask, "open_question_count", lambda _session: 1):
            await browser.wait("[...document.querySelectorAll('.uber-pane .open-questions')].some(e => e.checkVisibility())")
            fill["390x844"] = await fills_the_window(browser, "phone")
            await identity_on_touch(chrome, output, secret, await browser.evaluate("location.href"))
            await browser.shot(output, "beta-phone")
        await browser.evaluate("[...document.querySelectorAll('[aria-label=\"close the Ratchet chat\"]')].find(e => e.checkVisibility()).click()")
        await browser.choose_project(alpha["name"])
        await sidebar_chat(browser, alpha["name"])
        await browser.wait("[...document.querySelectorAll('.uber-pane')].some(e => e.checkVisibility() && e.getAttribute('aria-label').startsWith('Alpha'))")
        await browser.shot(output, "long-name-phone")
        # Issue 578: a neutral theme surface, not the project colour, with legible icons.
        toolbars = {}
        for scheme, menu in (("light", "colour scheme: follow the system"), ("dark", "colour scheme: light")):
            await browser.click(menu)
            await browser.click(scheme, ".v-list-item")
            await browser.wait(f"document.querySelector('.v-application.v-theme--{scheme}')")
            toolbars[scheme] = await browser.evaluate(TOOLBAR)
            toolbars[scheme]["contrast"] = contrast(toolbars[scheme]["background"], toolbars[scheme]["ink"])
        await browser.shot(output, "long-name-phone-dark")
        assert toolbars["light"]["background"] != toolbars["dark"]["background"], toolbars
        for toolbar in toolbars.values():
            assert toolbar["background"] != toolbar["bar"] and toolbar["contrast"] >= 4.5, toolbars
        await browser.evaluate("[...document.querySelectorAll('[aria-label=\"close the Ratchet chat\"]')].find(e => e.checkVisibility()).click()")
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.wait("document.querySelector('.v-navigation-drawer--left').getBoundingClientRect().left >= 0")
        for name in ("Browser Gamma", "Browser Delta"):
            await browser.switcher_entry("New project")
            await browser.wait("document.querySelector('.v-dialog input')")
            await browser.click_element("document.querySelector('.v-dialog input')")
            await browser.call("Input.insertText", text=name)
            await browser.click("Create project")
            await browser.wait(f"[...document.querySelectorAll('.uber-pane')].some(e => e.checkVisibility() && e.getAttribute('aria-label').startsWith({json.dumps(name)}))")
            created = [row for row in projects.listing() if row["name"] == name]
            assert len(created) == 1 and created[0]["repositories"] == []
            await asyncio.sleep(.5)
            await browser.shot(output, "new-project-dark-desktop")
            await browser.evaluate("[...document.querySelectorAll('[aria-label=\"close the Ratchet chat\"]')].find(e => e.checkVisibility()).click()")
        assert not browser.errors, browser.errors
        (output / "browser-results.json").write_text(json.dumps({
            "result": "pass", "viewports": ["1440x1000", "390x844", "3840x2160"], "fill": fill, "colors_saved": list(projects.COLORS), "label_contrast": contrasts, "uber_toolbar": toolbars,
            "checks": ["project-local All projects groups", "actual chat identity while All projects selected",
                       "durable waiting/cancel", "20 preset color-picker swatches saved by name through UI", "URL/color reload",
                       "close/switch/bar robot opens Beta", "queued historical draft survives admission", "staged attachment survives real rotation gap",
                       "phone overflow and long chat header", "uber pane fills 4K and phone height, badge corner reserved", "robot identity on a real tap and a mouse hover", "dark theme identity", "name-only project creation opens own chat",
                       "all project app bar text contrasts >= 4.5", "no runtime exceptions"],
            "limits": "Real Chromium and HTTP routes, fixture inventory and sleeping assistant processes; no external transports or live daemon.",
        }, indent=2) + "\n")


def main(probe_fn=probe, *, prepare=None, state_builder=None):
    executable, output = sys.argv[1], Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    for key in list(os.environ):
        if key.startswith("LMER_"):
            os.environ.pop(key)
    os.environ["LMER_PLATFORM_UI_DIST"] = str(Path(__file__).resolve().parents[2] / "dist")
    os.environ[config.ENV_CONTAINER_URL] = "http://isolated-browser.test:8600"
    with tempfile.TemporaryDirectory(prefix="project-browser-") as temporary:
        root = Path(temporary)
        store.PLATFORM_DIR = str(root / "platform")
        child = root / "lmer"
        child.write_text("#!/bin/sh\nexec sleep 600\n")
        child.chmod(0o755)
        settings = config.load({"lmer_bin": str(child)})
        secret = config.ensure_secret(settings)
        projects.migrate()
        config.update_stored({"max_live_assistants": 1})
        alpha, beta = [projects.create(name) for name in ("Alpha research with a deliberately long project name", "Beta")]
        projects.stop(alpha["id"])
        projects.reconcile(settings)
        assert projects.status(beta["id"])["running"]
        projects.stop(beta["id"])
        projects.request_start(alpha["id"])
        projects.request_start(beta["id"])
        projects.reconcile(settings)
        for project in (alpha, beta):
            with scope.bound(project["id"]):
                runs.track("forge.test", "shared/repository", "alpha-question" if project == alpha else "beta-question")
        def inventory(_config):
            rows = [{**run.to_dict(), "label": run.slug, "state": "waiting_on_you", "live": False,
                     "attention": {"reason": "question", "note": "Question one"}} for run in runs.list_tracked()]
            return {"runs": rows, "attention": rows, "sessions": [], "slots": [], "relations": [], "totals": {"runs": 2, "attention": 2}}
        if prepare:
            prepare(alpha, beta, settings)
        app = api.create_app(settings, secret, state_builder=state_builder or inventory)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        worker.start()
        chrome = subprocess.Popen([executable, "--headless", "--no-sandbox", "--disable-dev-shm-usage",
                                   "--remote-debugging-port=0", f"--user-data-dir={root / 'chrome'}", "about:blank"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 20
            active_port = root / "chrome" / "DevToolsActivePort"
            while (not server.started or not active_port.exists()) and time.monotonic() < deadline:
                time.sleep(.05)
            assert server.started and active_port.exists()
            debugging = int(active_port.read_text().splitlines()[0])
            asyncio.run(probe_fn(debugging, output, f"http://127.0.0.1:{port}/", secret, alpha, beta, settings))
            print((output / "browser-results.json").read_text())
        finally:
            chrome.terminate()
            chrome.wait(timeout=15)
            server.should_exit = True
            worker.join(timeout=15)
            listener.close()
            for entry in registry.list_sessions(live_only=True):
                os.killpg(entry["pid"], signal.SIGTERM)
                assert spawn.wait_for_exit_recorded(entry["id"])


if __name__ == "__main__":
    main()
