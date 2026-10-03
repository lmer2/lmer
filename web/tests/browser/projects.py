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

from lmer_platform import api, assistant, config, projects, registry, runs, spawn, store
from lmer_platform import project_context as scope


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

    async def click_element(self, expression):
        await self.evaluate(expression + ".scrollIntoView({block:'center'})")
        await asyncio.sleep(.5)
        position = await self.evaluate("(() => { const e = " + expression + "; const r = e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()")
        await self.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, **position)
        await self.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, **position)
        await asyncio.sleep(.2)

    async def click(self, label, selector="button"):
        target = f"[...document.querySelectorAll({json.dumps(selector)})].find(e => e.checkVisibility() && !e.closest('[inert]') && e.getBoundingClientRect().right > 0 && e.getBoundingClientRect().left < innerWidth && (e.getAttribute('aria-label') || e.textContent.trim()) === {json.dumps(label)})"
        if 'tab' in selector:
            await self.evaluate(target + ".focus()")
            await self.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r", unmodifiedText="\r")
            await self.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13)
            await asyncio.sleep(.3)
        else:
            await self.click_element(target)

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
            await browser.wait("document.body.textContent.includes('Restarting Beta · uber lmer')")
            assert await browser.evaluate("[...document.querySelectorAll('.attachments')].some(e => e.checkVisibility() && e.textContent.includes('rotation.txt'))")
            assert await browser.evaluate("[...document.querySelectorAll('[aria-label=\"send to this session\"]')].filter(e => e.checkVisibility()).every(e => e.disabled)")
            await browser.shot(output, 'rotation-gap-attachment')
        finally:
            release.set()
        after = rotation.result(timeout=20)
    assert after['running'] and after['session_id'] != during['history_session']
    await browser.wait("document.body.textContent.includes('You are talking to Beta')")
    await browser.wait("[...document.querySelectorAll('textarea')].some(e => e.checkVisibility() && !e.disabled)")
    assert await browser.evaluate("[...document.querySelectorAll('.attachments')].some(e => e.checkVisibility() && e.textContent.includes('rotation.txt'))")
    assert await browser.evaluate("[...document.querySelectorAll('textarea')].some(e => e.checkVisibility() && e.value === 'Beta draft retained through admission')")
    await browser.shot(output, 'rotation-successor-attachment')


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
        await browser.wait("document.querySelector('.project-chat-title')?.textContent.includes('Alpha')")
        await browser.click("All projects", '[role="tab"]')
        await browser.wait("document.querySelector('.project-identity').textContent.includes('All projects')")
        assert await browser.evaluate("[...document.querySelectorAll('.project-chat-title')].some(e => e.checkVisibility() && e.textContent.includes('Alpha'))")
        await browser.shot(output, "all-projects-bound-chat")
        await browser.evaluate("document.querySelector('[aria-label=\"close the uber lmer chat\"]').click()")
        await browser.click("Beta", '[role="tab"]')
        await browser.click("talk to uber lmer")
        await browser.wait("[...document.querySelectorAll('.project-chat-title')].some(e => e.checkVisibility() && e.textContent.includes('Beta'))")
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
        await browser.wait("document.body.textContent.includes('You are talking to Beta')")
        await browser.wait("[...document.querySelectorAll('textarea')].some(e => e.checkVisibility() && !e.disabled)")
        assert await browser.evaluate("[...document.querySelectorAll('textarea')].filter(e => e.checkVisibility() && !e.disabled).some(e => e.value === 'Beta draft retained through admission')")
        await browser.shot(output, "historical-draft-admitted")
        await rotating_composer(browser, output, beta, settings)
        await browser.evaluate("[...document.querySelectorAll('[aria-label=\"close the uber lmer chat\"]')].find(e => e.checkVisibility()).click()")
        await browser.click("Project settings for Beta")
        await browser.wait("document.querySelector('input[aria-label=\"Project color\"]') || document.body.textContent.includes('Project color')")
        contrasts = {}
        for color in projects.COLORS:
            await browser.click_element("[...document.querySelectorAll('.v-select')].find(e => e.textContent.includes('Project color')).querySelector('.v-field')")
            await browser.wait(f"[...document.querySelectorAll('[role=option]')].some(e => e.querySelector('.v-list-item-title')?.textContent.trim() === {json.dumps(color)})")
            await browser.click_element(f"[...document.querySelectorAll('[role=option]')].find(e => e.querySelector('.v-list-item-title')?.textContent.trim() === {json.dumps(color)})")
            await browser.click("Save project")
            await browser.wait(f"!document.querySelector('.v-btn--loading') && document.querySelector('.project-identity').textContent.includes('Beta')")
            assert projects.get(beta["id"])["color"] == color
            await browser.wait("getComputedStyle(document.querySelector('.project-identity .v-chip')).backgroundColor === getComputedStyle(document.querySelector('.v-select .v-field .v-chip')).backgroundColor")
            colors = await browser.evaluate("(() => { const style = getComputedStyle(document.querySelector('.project-identity .v-chip')); return [style.color, style.backgroundColor]; })()")
            def luminance(value):
                rgb = [int(number) / 255 for number in re.findall(r"\d+", value)[:3]]
                rgb = [part / 12.92 if part <= .04045 else ((part + .055) / 1.055) ** 2.4 for part in rgb]
                return sum(part * weight for part, weight in zip(rgb, (.2126, .7152, .0722)))
            foreground, background = map(luminance, colors)
            contrasts[color] = (max(foreground, background) + .05) / (min(foreground, background) + .05)
            assert contrasts[color] >= 4.5, (color, colors, contrasts[color])
        await browser.call("Page.reload")
        await browser.wait("document.querySelector('.project-identity')?.textContent.includes('Beta')")
        assert projects.get(beta["id"])["color"] == "Gray"
        await browser.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
        await browser.wait("document.querySelector('[aria-label=\"show the run list\"]')")
        await browser.click("Chat with Beta")
        await browser.wait("[...document.querySelectorAll('.project-chat-title')].some(e => e.checkVisibility() && e.textContent.includes('Beta'))")
        overflow = await browser.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert not overflow, "Page overflows the phone viewport"
        await browser.shot(output, "beta-phone")
        await browser.evaluate("[...document.querySelectorAll('[aria-label=\"close the uber lmer chat\"]')].find(e => e.checkVisibility()).click()")
        await browser.click(alpha["name"], '[role="tab"]')
        await browser.click("Chat with " + alpha["name"])
        await browser.wait("[...document.querySelectorAll('.project-chat-title')].some(e => e.checkVisibility() && e.textContent.includes('Alpha'))")
        await browser.shot(output, "long-name-phone")
        await browser.click("colour scheme: follow the system")
        await browser.click("dark", ".v-list-item")
        await browser.wait("document.querySelector('.v-application.v-theme--dark')")
        await browser.shot(output, "long-name-phone-dark")
        assert await browser.evaluate("getComputedStyle(document.querySelector('.assistant-header')).color") == 'rgb(255, 255, 255)'
        await browser.evaluate("[...document.querySelectorAll('[aria-label=\"close the uber lmer chat\"]')].find(e => e.checkVisibility()).click()")
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.wait("document.querySelector('.v-navigation-drawer--left').getBoundingClientRect().left >= 0")
        for name in ("Browser Gamma", "Browser Delta"):
            await browser.click("New project")
            await browser.wait("document.querySelector('.v-dialog input')")
            await browser.click_element("document.querySelector('.v-dialog input')")
            await browser.call("Input.insertText", text=name)
            await browser.click("Create project")
            await browser.wait(f"[...document.querySelectorAll('.project-chat-title')].some(e => e.checkVisibility() && e.textContent.includes({json.dumps(name)}))")
            created = [row for row in projects.listing() if row["name"] == name]
            assert len(created) == 1 and created[0]["repositories"] == []
            await asyncio.sleep(.5)
            await browser.shot(output, "new-project-dark-desktop")
            await browser.evaluate("[...document.querySelectorAll('[aria-label=\"close the uber lmer chat\"]')].find(e => e.checkVisibility()).click()")
        assert not browser.errors, browser.errors
        (output / "browser-results.json").write_text(json.dumps({
            "result": "pass", "viewports": ["1440x1000", "390x844"], "colors_saved": list(projects.COLORS), "label_contrast": contrasts,
            "checks": ["project-local All projects groups", "actual chat identity while All projects selected",
                       "durable waiting/cancel", "20 named swatches saved through UI", "URL/color reload",
                       "close/switch/toolbar opens Beta", "queued historical draft survives admission", "staged attachment survives real rotation gap",
                       "phone overflow and long chat header", "dark theme identity", "name-only project creation opens own chat",
                       "all project label contrasts >= 4.5", "no runtime exceptions"],
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
