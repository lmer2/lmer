"""Issue 663: Enter sends, Shift+Enter breaks the line, and touch keeps Enter a newline.

Against the built UI and real platform routes, at laptop, tablet and phone widths.
Screenshots the Chat and display setting and a live composer at each width, and
drives real key events (not synthetic DOM events) at the composer.

Usage: python web/tests/browser/enter_to_send.py CHROMIUM OUTPUT
"""
import asyncio
import json

from websockets.asyncio.client import connect

import projects as fixture

WIDTHS = [
    # name, width, height, touch
    ("laptop", 1440, 1000, False),
    ("tablet", 820, 1180, True),
    ("phone", 390, 844, True),
]
COMPOSER = "[...document.querySelectorAll('.composer textarea')].find(e => e.checkVisibility() && !e.disabled)"


async def enter(browser, shift=False):
    params = dict(key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r", modifiers=8 if shift else 0)
    await browser.call("Input.dispatchKeyEvent", type="keyDown", **params)
    await browser.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter",
                       windowsVirtualKeyCode=13, modifiers=params["modifiers"])
    await asyncio.sleep(.3)


async def device(browser, width, height, touch):
    await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                       deviceScaleFactor=1, mobile=touch)
    await browser.call("Emulation.setTouchEmulationEnabled", enabled=touch, maxTouchPoints=5 if touch else 1)
    await browser.reload()
    await browser.wait("document.querySelector('header [aria-label=\"Platform settings\"]')")


async def setting(browser, output, name):
    await browser.click("Platform settings")
    await browser.settings_section("Chat and display")
    await browser.wait("document.querySelector('[data-panel=\"chat\"] .enter-sends')?.checkVisibility()")
    await browser.evaluate("document.querySelector('[data-panel=\"chat\"] .enter-sends').scrollIntoView({block: 'center'})")
    await asyncio.sleep(.5)
    on = await browser.evaluate("document.querySelector('[data-panel=\"chat\"] .enter-sends input').checked")
    await browser.shot(output, f"setting-{name}")
    await browser.click("back to the fleet")
    await browser.wait("!document.querySelector('.platform-settings')")
    return on


async def composer(browser, output, name, alpha):
    await fixture.sidebar_chat(browser, alpha)
    await browser.wait(COMPOSER)
    await browser.click_element(COMPOSER)
    await browser.call("Input.insertText", text="first line")
    await enter(browser, shift=True)
    await browser.call("Input.insertText", text="second line")
    after_shift = await browser.evaluate(f"{COMPOSER}.value")
    hint = await browser.evaluate(f"{COMPOSER}.closest('.v-input').querySelector('.v-messages').textContent")
    coarse = await browser.evaluate("matchMedia('(pointer: coarse)').matches")
    await browser.shot(output, f"composer-{name}")
    await enter(browser)
    await asyncio.sleep(1)
    after_enter = await browser.evaluate(f"({COMPOSER})?.value ?? null")
    # The isolated session is a sleeping child with no control plane, so a send
    # reaches POST /input and comes back refused; the composer keeps the draft for
    # a retry. That refusal is the evidence Enter sent.
    attempted = await browser.evaluate("document.body.textContent.includes('not sent')")
    await browser.shot(output, f"composer-{name}-after-enter")
    return {"coarse": coarse, "hint": hint.strip(), "after_shift_enter": after_shift,
            "after_enter": after_enter, "send_attempted": attempted}


async def probe(chrome, output, url, secret, alpha, beta, settings):
    targets = json.load(fixture.urllib.request.urlopen(f"http://127.0.0.1:{chrome}/json/list", timeout=5))
    page = next(target for target in targets if target["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as connection:
        browser = fixture.Browser(connection)
        browser.output = output
        await browser.call("Runtime.enable")
        await browser.call("Network.enable")
        await browser.call("Network.setExtraHTTPHeaders", headers={"Authorization": f"Bearer {secret}"})
        await browser.call("Page.navigate", url=url)
        results = {}
        for name, width, height, touch in WIDTHS:
            await device(browser, width, height, touch)
            row = {"setting_on": await setting(browser, output, name)}
            row.update(await composer(browser, output, name, alpha["name"]))
            assert row["setting_on"], row
            assert row["coarse"] == touch, row
            assert row["after_shift_enter"] == "first line\nsecond line", row
            if touch:
                # Enter is the phone's only newline key: it must break the line, not send.
                assert row["after_enter"] == "first line\nsecond line\n", row
                assert not row["send_attempted"], row
                assert row["hint"].startswith("Tap send below — Enter is a new line"), row
            else:
                # Sent, and the keystroke's newline was suppressed rather than
                # left in the draft the failed send keeps.
                assert row["after_enter"] == "first line\nsecond line", row
                assert row["send_attempted"], row
                assert row["hint"].startswith("Tap send below — Enter sends, Shift+Enter is a new line"), row
            results[name] = row
        assert not browser.errors, browser.errors
        (output / "browser-results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    fixture.main(probe)
