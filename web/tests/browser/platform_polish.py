"""Issue 646's platform polish against the built UI, real HTTP routes and sleeping assistants.

Usage: python web/tests/browser/platform_polish.py CHROMIUM OUTPUT
"""
import asyncio
import json
import re
import urllib.request

from websockets.asyncio.client import connect

import projects as fixture
from lmer_platform import assistant_history, config, projects, runs

OPERATOR_TEXT = "Ship the polish pass"
PURPOSE = "GitLab deploy token"

PANE = "[...document.querySelectorAll('.uber-pane')].find(e => e.checkVisibility())"

GROUND = """(() => {
  const pane = %s;
  const turn = [...pane.querySelectorAll('.turn.ground-operator')].find(e => e.textContent.includes(%s));
  const ink = (e) => { const s = getComputedStyle(e); return {color: s.color, opacity: s.opacity}; };
  return {ground: getComputedStyle(turn).backgroundColor,
          head: ink(turn.querySelector('.text-medium-emphasis')), body: ink(turn.querySelector('.said'))};
})()""" % (PANE, json.dumps(OPERATOR_TEXT))


def prepare(alpha, beta, settings):
    for run in runs.list_tracked():
        runs.forget(run.host, run.project, run.slug)
    config.update_stored({"max_live_assistants": 4})
    for project, name in ((alpha, "Alpha"), (beta, "Beta")):
        projects.update(project["id"], {"name": name})
        project["name"] = name
        projects.request_start(project["id"])
    projects.reconcile(settings)
    session = projects.status(alpha["id"])["session_id"]
    for record in (
        {"id": "agent", "kind": "message", "text": "Ready for the next step.", "source": {"channel": "agent"}},
        {"id": "operator", "kind": "message", "text": OPERATOR_TEXT, "source": {"channel": "platform", "author": "operator"}},
        {"id": "private", "kind": "message", "source": {"channel": "platform", "author": "operator"},
         "meta": {"private": {"purpose": PURPOSE}},
         "text": "[lmer private] /home/developer/.lmer-uploads/.lmer-private/s/20261030-120000-private-gitlab-deploy-token.txt"
                 " — private text for: GitLab deploy token (this session only). Use it in place without printing it"},
    ):
        assistant_history.retain(session, record)


def _rgba(value):
    """Channels 0-255 and alpha, from ``rgb()``/``rgba()`` or Chromium's ``color(srgb r g b / a)``."""
    numbers = [float(n) for n in re.findall(r"[\d.]+", value)]
    scale = 255 if value.startswith("color(srgb") else 1
    return [n * scale for n in numbers[:3]], (numbers[3] if len(numbers) > 3 else 1.0)


def ink_on(ink, ground):
    """The opaque color an emphasis-faded ink paints on its ground."""
    rgb, alpha = _rgba(ink["color"])
    alpha *= float(ink["opacity"])
    base, _ = _rgba(ground)
    return "rgb(%s)" % ",".join(str(round(alpha * c + (1 - alpha) * b)) for c, b in zip(rgb, base))


async def open_chat(browser, name):
    if await browser.evaluate(f"!!{PANE} && {PANE}.getAttribute('aria-label').startsWith({json.dumps(name)})"):
        return
    await browser.click_element(f"document.querySelector('.project-assistant[aria-label^=\"Chat with {name}\"]')")
    await browser.wait(f"{PANE}?.getAttribute('aria-label').startsWith({json.dumps(name)})")


async def scheme(browser, name):
    current = await browser.evaluate("[...document.querySelectorAll('header button')].find(e => (e.getAttribute('aria-label') || '').startsWith('colour scheme'))?.getAttribute('aria-label')")
    await browser.click(current)
    await browser.click(name, ".v-list-item")
    await browser.wait(f"document.querySelector('.v-application.v-theme--{name}')")


async def operator_contrast(browser, alpha):
    results = {}
    for name in ("light", "dark"):
        await scheme(browser, name)
        for color in [*projects.COLORS, "#ffffff", "#ffff00", "#1a1a40"]:
            projects.update(alpha["id"], {"color": color})
            await browser.call("Page.reload")
            await browser.wait("document.querySelector('.project-assistant[aria-label^=\"Chat with Alpha\"]')")
            await open_chat(browser, "Alpha")
            await browser.wait(f"[...{PANE}.querySelectorAll('.turn.ground-operator')].some(e => e.textContent.includes({json.dumps(OPERATOR_TEXT)}))")
            measured = await browser.evaluate(GROUND)
            row = {part: fixture.contrast(ink_on(measured[part], measured["ground"]), measured["ground"]) for part in ("head", "body")}
            row["ground"] = measured["ground"]
            results[f"{name}:{color}"] = row
            assert row["head"] >= 4.5 and row["body"] >= 4.5, (name, color, row, measured)
    grounds = {key: row["ground"] for key, row in results.items()}
    assert grounds["light:Red"] != grounds["light:Teal"] and grounds["dark:Red"] != grounds["dark:Teal"], grounds
    return results


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
        await browser.wait("document.querySelector('.project-assistant[aria-label^=\"Chat with Alpha\"]')")
        report = {}

        # Item 9: every preset hue and a few custom ones, both schemes, AA for body and header.
        report["operator_contrast"] = await operator_contrast(browser, alpha)
        projects.update(alpha["id"], {"color": "Blue"})
        await browser.call("Page.reload")
        await browser.wait("document.querySelector('.project-assistant[aria-label^=\"Chat with Alpha\"]')")
        await open_chat(browser, "Alpha")
        await browser.shot(output, "operator-tint-dark-desktop")
        await scheme(browser, "light")

        # Items 3 and 8: the private entry and the copy control.
        await browser.wait(f"{PANE}.querySelector('.private-entry')")
        entry = await browser.evaluate(f"{PANE}.querySelector('.private-entry').closest('.turn').textContent")
        assert f"Private message sent: {PURPOSE}" in entry and "lmer-private" not in entry and "Use it in place" not in entry, entry
        copy = await browser.evaluate(f"""(() => {{
          const turn = [...{PANE}.querySelectorAll('.turn')].find(e => e.textContent.includes({json.dumps(OPERATOR_TEXT)}));
          const button = turn.querySelector('.copy-button'); const head = button.parentElement;
          const b = button.getBoundingClientRect(); const h = head.getBoundingClientRect();
          return {{width: b.width, height: b.height, header: h.height, background: getComputedStyle(button).backgroundColor,
                   icon: button.querySelector('svg').getBoundingClientRect().width, label: button.getAttribute('aria-label')}};
        }})()""")
        assert copy["width"] == 24 and copy["height"] == 24 and copy["icon"] == 18 and copy["header"] <= 24, copy
        assert copy["background"] in ("rgba(0, 0, 0, 0)", "transparent") and copy["label"] == "Copy message", copy
        report["copy_button"] = copy
        await browser.shot(output, "private-entry-and-copy-light")

        # Item 7 at desktop width: lock and label, the same size and style as attach.
        composer = """(() => {
          const pane = %s; const row = pane.querySelector('.send-row');
          const attach = row.querySelector('[aria-label="attach a file to this message"]');
          const privately = row.querySelector('[aria-label="Send privately"]');
          const label = privately.querySelector('.private-label');
          return {attach: attach.getBoundingClientRect().height, private: privately.getBoundingClientRect().height,
                  privateWidth: privately.getBoundingClientRect().width, attachWidth: attach.getBoundingClientRect().width,
                  label: label.checkVisibility() ? label.textContent.trim() : null,
                  composer: pane.querySelector('.composer').getBoundingClientRect().width};
        })()""" % PANE
        desktop = await browser.evaluate(composer)
        assert desktop["private"] == desktop["attach"], desktop
        report["composer_desktop"] = desktop

        # Item 1: the daemon answers 502; the error is a line, the conversation stays.
        await browser.evaluate("""(() => {
          const original = window.fetch;
          window.fetch = (input, init) => /api\\/assistant(\\/history|\\?|$)/.test(String(input?.url || input))
            ? Promise.resolve(new Response(JSON.stringify({detail: 'HTTP 502'}), {status: 502, headers: {'content-type': 'application/json'}}))
            : original(input, init);
        })()""")
        await browser.wait(f"[...{PANE}.querySelectorAll('.chat-fill .v-alert')].some(e => e.checkVisibility())")
        await asyncio.sleep(1)
        error = await browser.evaluate(f"""(() => {{
          const pane = {PANE};
          const alert = [...pane.querySelectorAll('.chat-fill .v-alert')].find(e => e.checkVisibility());
          const turn = [...pane.querySelectorAll('.turn')].find(e => e.textContent.includes({json.dumps(OPERATOR_TEXT)}));
          return {{alert: alert.getBoundingClientRect().height, text: alert.textContent.trim(),
                   chat: pane.querySelector('.chat').getBoundingClientRect().height, turn: turn.checkVisibility(),
                   banner: [...pane.querySelectorAll('.uber-notices .v-alert')].map(e => e.textContent.trim())}};
        }})()""")
        assert error["alert"] <= 80 and error["turn"] and error["chat"] > 300, error
        report["forced_502"] = error
        await browser.shot(output, "forced-502-desktop")
        await browser.call("Page.reload")
        await browser.wait("document.querySelector('.project-assistant[aria-label^=\"Chat with Alpha\"]')")

        # Items 5 and 6 and the effort field (item 4c): grid, picker and keyboard order.
        await browser.choose_project("Alpha")
        layouts = {}
        for width, height, mobile in ((1440, 1000, False), (360, 800, True)):
            await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=mobile)
            await browser.switcher_entry("Project settings for Alpha")
            await browser.wait("document.querySelector('.project-settings .settings-grid')")
            await asyncio.sleep(.5)
            layout = await browser.evaluate("""(() => {
              const card = document.querySelector('.project-settings');
              const field = (label) => [...card.querySelectorAll('.v-input')].find(e => e.querySelector('label')?.textContent.trim() === label).getBoundingClientRect();
              const [model, harness, effort] = ['Model', 'Harness', 'Reasoning effort'].map(field);
              const picker = card.querySelector('.project-color .v-color-picker').getBoundingClientRect();
              const section = card.querySelector('.project-color').getBoundingClientRect();
              return {card: card.scrollHeight, model: [model.left, model.top], harness: [harness.left, harness.top],
                      effort: [effort.left, effort.top], picker: [picker.width, picker.height],
                      pickerCentre: picker.left + picker.width / 2, sectionCentre: section.left + section.width / 2,
                      overflow: document.documentElement.scrollWidth > innerWidth || card.scrollWidth > card.clientWidth};
            })()""")
            assert not layout["overflow"], layout
            assert abs(layout["pickerCentre"] - layout["sectionCentre"]) <= 2 and layout["picker"][1] <= 160, layout
            if mobile:
                assert layout["harness"][1] > layout["model"][1] and layout["harness"][0] == layout["model"][0], layout
            else:
                assert layout["harness"][1] == layout["model"][1] and layout["harness"][0] > layout["model"][0], layout
            await browser.click_element("[...document.querySelectorAll('.project-settings .v-input')].find(e => e.querySelector('label')?.textContent.trim() === 'Model').querySelector('input')")
            order = []
            for _ in range(5):
                order.append(await browser.evaluate("document.activeElement.closest('.v-input')?.querySelector('label')?.textContent.trim()"))
                await browser.call("Input.dispatchKeyEvent", type="keyDown", key="Tab", code="Tab", windowsVirtualKeyCode=9)
                await browser.call("Input.dispatchKeyEvent", type="keyUp", key="Tab", code="Tab", windowsVirtualKeyCode=9)
            assert order == ["Model", "Harness", "Preset", "Fan-out presets", "Reasoning effort"], order
            layout["tab_order"] = order
            layouts[f"{width}x{height}"] = layout
            await browser.shot(output, f"project-settings-{width}")
            await browser.click("Close")
            await browser.wait("!document.querySelector('.project-settings')")
        report["settings"] = layouts

        # Item 7 at phone width: the lock alone, named and the same size as attach.
        await browser.call("Page.reload")
        await browser.wait("document.querySelector('[aria-label=\"show the run list\"]')")
        await fixture.sidebar_chat(browser, "Alpha")
        await browser.wait(f"{PANE}?.querySelector('.send-row')")
        phone = await browser.evaluate(composer)
        assert phone["label"] is None and phone["private"] == phone["attach"], phone
        assert not await browser.evaluate("document.documentElement.scrollWidth > innerWidth")
        report["composer_phone"] = phone
        await browser.shot(output, "composer-phone")
        await browser.evaluate("[...document.querySelectorAll('[aria-label=\"close the Ratchet chat\"]')].find(e => e.checkVisibility()).click()")

        # Item 2 on a phone: the fleet bar's row keeps its 320 px budget (issue 596),
        # so the request is under Platform settings instead of in the bar.
        bars = {}
        for width in (320, 360):
            await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=800, deviceScaleFactor=1, mobile=True)
            await browser.call("Page.reload")
            await browser.wait("document.querySelector('header .bar-row [aria-label=\"read the fleet as a list\"]')")
            await asyncio.sleep(.5)
            bar = await browser.evaluate("""(() => {
              const buttons = [...document.querySelectorAll('header .bar-row button')].filter(e => e.checkVisibility());
              const outside = buttons.filter(e => { const r = e.getBoundingClientRect(); return r.left < 0 || r.right > innerWidth; });
              return {buttons: buttons.map(e => e.getAttribute('aria-label')), outside: outside.map(e => e.getAttribute('aria-label')),
                      rotate: !!document.querySelector('header .rotate-all'),
                      settingsRight: document.querySelector('header [aria-label="Platform settings"]').getBoundingClientRect().right,
                      overflow: document.documentElement.scrollWidth > innerWidth};
            })()""")
            assert not bar["rotate"] and not bar["outside"] and not bar["overflow"], bar
            assert "rescan now" in bar["buttons"] and bar["settingsRight"] <= width, bar
            bars[width] = bar
            await browser.shot(output, f"fleet-bar-phone-{width}")
        await browser.click("Platform settings")
        await browser.click("Ask every Ratchet to rotate")
        await browser.wait("document.querySelector('.rotate-all-dialog')?.textContent.includes('Beta · generation')")
        assert not await browser.evaluate("document.documentElement.scrollWidth > innerWidth")
        await browser.shot(output, "rotate-all-dialog-phone")
        await browser.click("cancel")
        await browser.wait("!document.querySelector('.rotate-all-dialog')?.checkVisibility()")
        report["phone_bar"] = bars

        # Item 2: the header button lists both assistants, asks, and reports each one.
        await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.call("Page.reload")
        await browser.wait("document.querySelector('header .rotate-all')")
        await browser.click("Ask every Ratchet to rotate")
        await browser.wait("document.querySelector('.rotate-all-dialog')?.textContent.includes('Beta · generation')")
        listed = await browser.evaluate("document.querySelector('.rotate-all-dialog').textContent")
        assert "Alpha · generation" in listed and "Beta · generation" in listed, listed
        await browser.click("ask 2 to rotate")
        await browser.wait("document.querySelectorAll('.rotate-all-dialog [data-state]').length >= 2")
        states = await browser.evaluate("Object.fromEntries([...document.querySelectorAll('.rotate-all-dialog [data-state]')].map(e => [e.textContent.trim().split(' ·')[0], e.dataset.state]))")
        # These assistants are sleeping processes with no control plane: they cannot be
        # reached, which the modal says per instance, and neither was stopped.
        assert {states.get("Alpha"), states.get("Beta")} <= {"not_reached", "failed"}, states
        assert projects.status(alpha["id"])["running"] and projects.status(beta["id"])["running"]
        report["rotate_all"] = states
        await browser.shot(output, "rotate-all-dialog")

        assert not browser.errors, browser.errors
        (output / "browser-results.json").write_text(json.dumps({"result": "pass", **report}, indent=2) + "\n")


if __name__ == "__main__":
    fixture.main(probe, prepare=prepare)
