"""Project settings' mention channels (issue #546) against isolated stores/HTTP.

Usage: python this_file.py CHROMIUM OUTPUT
IDs and room links only, so no Slack or Matrix call is made.
"""
import asyncio
import json
import sys
import urllib.request

from websockets.asyncio.client import connect

import projects as fixture
from project_controls import input_text, inventory, prepare
from lmer_platform import channel_routes, projects

FIELD = "document.querySelector('.mention-channels textarea')"
ALERT = "document.querySelector('.project-settings .v-alert')?.textContent || ''"


async def channels_screen(browser, output, width, alpha):
    await browser.switcher_entry("Project settings for Example Beta")
    await browser.wait(FIELD + " && !" + FIELD + ".disabled")
    await input_text(browser, FIELD, f"C0BETA{width}\nhttps://matrix.to/#/!beta{width}:example.org/$event")
    await browser.click("Save project")
    await browser.wait("!document.querySelector('.v-btn--loading')")
    assert not await browser.evaluate(ALERT), await browser.evaluate(ALERT)
    shown = await browser.evaluate(FIELD + ".value")
    assert shown == f"C0BETA{width}\n!beta{width}:example.org", (shown, channel_routes.listing())
    held = {(route["conversation"], route["project_name"]) for route in channel_routes.listing()}
    assert (f"C0BETA{width}", "Example Beta") in held and (f"!beta{width}:example.org", "Example Beta") in held, held
    await browser.evaluate(FIELD + ".scrollIntoView({block:'center'})")
    await asyncio.sleep(.3)
    await browser.shot(output, f"mention-channels-{width}")
    await input_text(browser, FIELD, "C0ALPHA01")
    await browser.click("Save project")
    await browser.wait("document.querySelector('.project-settings .v-alert')")
    assert "already mapped to project Example Alpha" in await browser.evaluate(ALERT)
    await browser.evaluate("document.querySelector('.project-settings .v-alert').scrollIntoView({block:'center'})")
    await asyncio.sleep(.3)
    await browser.shot(output, f"mention-channels-conflict-{width}")
    assert not await browser.evaluate("document.documentElement.scrollWidth > innerWidth"), width
    await browser.click("Close")


async def probe(chrome, output, url, secret, alpha, beta, settings):
    channel_routes.replace(alpha["id"], ["C0ALPHA01"])
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
        await browser.wait("document.querySelectorAll('.project-nav').length === 4")
        await browser.choose_project("Example Beta")
        await channels_screen(browser, output, 1440, alpha)
        for width in (390, 320):
            await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=844, deviceScaleFactor=1, mobile=True)
            await browser.wait("document.querySelector('[aria-label=\"show the run list\"]')")
            await channels_screen(browser, output, width, alpha)
        assert not browser.errors, browser.errors
    assert {route["project_name"] for route in channel_routes.listing()} == {"Example Alpha", "Example Beta"}
    (output / "browser-results.json").write_text(json.dumps({
        "result": "pass", "viewports": ["1440x1000", "390x844", "320x844"], "runtime_errors": browser.errors,
        "checks": ["mention channels save, resolve a room link and re-render resolved labels",
                   "a channel held by another project is refused with its name visible", "no horizontal overflow"],
        "limits": "Real browser and temporary HTTP/stores; IDs and links only, so no transport is contacted.",
    }, indent=2) + "\n")


if __name__ == "__main__":
    fixture.main(probe, prepare=prepare, state_builder=inventory)
