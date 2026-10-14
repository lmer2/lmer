"""Isolated sidebar/header acceptance. Usage: python this_file.py CHROMIUM OUTPUT."""
import asyncio
import json
import urllib.request

from websockets.asyncio.client import connect

import projects as fixture
from lmer_platform import projects, runs
from lmer_platform import project_context as scope


async def key(browser, key, code, number, text):
    await browser.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=code,
                       windowsVirtualKeyCode=number, text=text, unmodifiedText=text)
    await browser.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=code, windowsVirtualKeyCode=number)


async def toolbar(browser, output, names, *, keyboard=False):
    label = 'project: Beta — switch project'
    switcher, menu = browser.SWITCHER, browser.SWITCHER_LIST
    entries = ['All projects', *names, 'Project settings for Beta', 'New project']
    assert await browser.evaluate("document.querySelectorAll('.project-switcher').length") == 1
    assert await browser.evaluate(switcher + ".closest('header.v-app-bar') !== null")
    assert await browser.evaluate(switcher + ".textContent.trim()") == 'Beta'
    assert await browser.evaluate(switcher + ".getAttribute('aria-label')") == label
    assert await browser.evaluate(switcher + ".getAttribute('aria-haspopup')") == 'menu'
    assert await browser.evaluate("(() => { const r = " + switcher + ".getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.height >= 36; })()")
    if keyboard:
        await key(browser, 'Tab', 'Tab', 9, '')
        await browser.evaluate(switcher + '.focus()')
        assert await browser.evaluate("document.activeElement.getAttribute('aria-label')") == label
        await browser.wait("(() => { const tip = document.getElementById(document.activeElement.getAttribute('aria-describedby')); const content = tip?.querySelector('.v-overlay__content'); if (!content) return false; const style = getComputedStyle(content); return tip.classList.contains('v-overlay--active') && style.opacity === '1' && style.transform === 'none' && tip.textContent.trim() === " + json.dumps(label) + "; })()")
        await browser.shot(output, 'switcher-keyboard-tooltip')
        await key(browser, 'Enter', 'Enter', 13, '\r')
        await browser.wait(menu)
        assert await browser.evaluate(switcher + ".getAttribute('aria-expanded')") == 'true'
        current = "document.activeElement.closest('.project-switcher-list') && document.activeElement.querySelector('.v-list-item-title')?.textContent.trim()"
        # Arrow keys reach every entry once per lap, in list order (the divider is
        # skipped); Vuetify starts the lap after the selected entry, so compare as a cycle.
        focused = []
        for _ in entries:
            await key(browser, 'ArrowDown', 'ArrowDown', 40, '')
            await browser.wait(current)
            focused.append(await browser.evaluate(current))
        assert focused[0] in entries, focused
        lap = entries.index(focused[0])
        assert focused == entries[lap:] + entries[:lap], focused
        while await browser.evaluate(current) != 'New project':
            await key(browser, 'ArrowDown', 'ArrowDown', 40, '')
    else:
        await browser.click_element(switcher)
        await browser.wait(menu)
    assert await browser.evaluate("[..." + menu + ".querySelectorAll('.v-list-item')].map(e => e.querySelector('.v-list-item-title').textContent.trim())") == entries
    assert await browser.evaluate("[..." + menu + ".querySelectorAll('.v-list-item--active')].map(e => e.querySelector('.v-list-item-title').textContent.trim())") == ['Beta']
    assert await browser.evaluate("(() => { const r = " + menu + ".getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })()")
    await browser.wait("getComputedStyle(" + menu + ".closest('.v-overlay__content')).opacity === '1'")
    await browser.shot(output, 'switcher-menu-' + ('desktop' if keyboard else 'phone'))
    if keyboard:
        await key(browser, 'Enter', 'Enter', 13, '\r')
    else:
        await browser.click_element("[..." + menu + ".querySelectorAll('.v-list-item')].find(e => e.textContent.trim() === 'New project')")
    await browser.wait("document.querySelector('.v-dialog input')")
    await browser.wait("!" + menu)
    await browser.shot(output, 'switcher-new-project-dialog-' + ('desktop' if keyboard else 'phone'))
    await browser.click('Cancel')


async def probe(chrome, output, url, secret, alpha, beta, settings):
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
        await browser.wait("document.querySelectorAll('.project-nav').length === 3")
        names = [row["name"] for row in projects.listing(archived=False)]
        await browser.choose_project("Beta")
        await browser.wait("document.querySelector('.project-nav-selected')?.getAttribute('aria-label') === 'Beta'")
        sidebar = "document.querySelector('.v-navigation-drawer--left')"
        alpha_group = "document.querySelector('.project-nav[aria-label^=\"Alpha\"]')"
        beta_group = "document.querySelector('.project-nav[aria-label=\"Beta\"]')"
        general_group = "document.querySelector('.project-nav[aria-label=\"General\"]')"
        assert await browser.evaluate("document.querySelectorAll('.project-nav').length") == 3
        assert await browser.evaluate(alpha_group + ".textContent.includes('alpha-question')")
        assert not await browser.evaluate(alpha_group + ".textContent.includes('beta-question')")
        assert await browser.evaluate(beta_group + ".textContent.includes('beta-question')")
        assert await browser.evaluate(general_group + ".textContent.includes('nothing tracked yet')")
        assert not await browser.evaluate("document.querySelector('main').textContent.includes('alpha-question')")
        assert await browser.evaluate("location.search") == "?workspace=" + beta["id"]
        assert await browser.evaluate("document.querySelectorAll('[aria-label=\"Platform settings\"]').length") == 1
        assert not await browser.evaluate(sidebar + ".textContent.includes('Platform settings')")
        await browser.shot(output, "selected-beta-desktop")
        await toolbar(browser, output, names, keyboard=True)

        toggle = alpha_group + ".querySelector('.project-toggle')"
        await browser.evaluate(toggle + ".focus()")
        await key(browser, "Enter", "Enter", 13, "\r")
        await browser.wait(toggle + ".getAttribute('aria-expanded') === 'false'")
        assert await browser.evaluate(beta_group + ".querySelector('.project-toggle').getAttribute('aria-expanded')") == 'true'
        await browser.shot(output, "independent-collapse-desktop")
        chat = alpha_group + ".querySelector('.project-assistant')"
        await browser.evaluate(chat + ".focus()")
        await key(browser, " ", "Space", 32, " ")
        await browser.wait("[...document.querySelectorAll('.uber-pane')].some(e => e.checkVisibility() && e.getAttribute('aria-label').startsWith('Alpha'))")
        await browser.wait("document.querySelector('.v-navigation-drawer--right').getBoundingClientRect().right <= innerWidth + 1")
        assert await browser.evaluate(toggle + ".getAttribute('aria-expanded')") == 'false'
        assert await browser.evaluate("location.search") == "?workspace=" + alpha["id"]
        await browser.shot(output, "collapsed-project-chat-desktop")
        await browser.click("close the uber lmer chat")
        await browser.wait("document.querySelector('.v-navigation-drawer--right').getBoundingClientRect().left >= innerWidth")
        await browser.choose_project("Beta")
        with scope.bound(alpha["id"]):
            runs.track("forge.test", "shared/repository", "alpha-extra-question")
        await browser.evaluate("window.dispatchEvent(new Event('focus'))")
        await browser.wait(alpha_group + ".querySelector('.project-attention').textContent === '2'")
        assert await browser.evaluate(beta_group + ".querySelector('.project-attention').textContent") == '1'
        assert await browser.evaluate(toggle + ".getAttribute('aria-expanded')") == 'false'
        await browser.evaluate(toggle + ".focus()")
        await key(browser, " ", "Space", 32, " ")
        await browser.wait(toggle + ".getAttribute('aria-expanded') === 'true'")
        await browser.choose_project("All projects")
        await browser.wait("document.querySelector('main').textContent.includes('alpha-extra-question')")
        assert await browser.evaluate("document.querySelectorAll('.project-nav').length") == 3
        await browser.shot(output, "all-projects-desktop")
        await browser.choose_project("Beta")
        await browser.click("Platform settings")
        await browser.wait("document.querySelector('h1')?.textContent === 'Platform settings'")
        await browser.shot(output, "global-settings-desktop")
        await browser.click("back to the fleet")
        await browser.switcher_entry("New project")
        await browser.wait("document.querySelector('.v-dialog input')")
        await browser.shot(output, "new-project-dialog-desktop")
        await browser.click("Cancel")
        await browser.click("colour scheme: follow the system")
        await browser.click("dark", ".v-list-item")
        await browser.wait("document.querySelector('.v-application.v-theme--dark')")
        await browser.shot(output, "selected-beta-desktop-dark")

        for width in (390, 320):
            await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=844, deviceScaleFactor=1, mobile=True)
            await browser.wait("document.querySelector('[aria-label=\"show the run list\"]')")
            await browser.wait("parseFloat(getComputedStyle(document.querySelector('main')).paddingLeft) === 0")
            if await browser.evaluate(sidebar + ".classList.contains('v-navigation-drawer--active')"):
                await browser.click("show the run list")
            await browser.wait(sidebar + ".getBoundingClientRect().right <= 0")
            assert not await browser.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert await browser.evaluate("(() => { const r = document.querySelector('header [aria-label=\"Platform settings\"]').getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && r.top >= 0; })()")
            await browser.shot(output, f"selected-beta-phone-{width}")
            await toolbar(browser, output, names)
            await browser.click("Platform settings")
            await browser.wait("document.querySelector('h1')?.textContent === 'Platform settings'")
            await browser.wait("document.querySelector('.v-navigation-drawer--right').getBoundingClientRect().left >= innerWidth")
            await browser.shot(output, f"global-settings-phone-{width}")
            await browser.click("back to the fleet")
            await browser.click("show the run list")
            await browser.wait(sidebar + ".getBoundingClientRect().left >= 0")
            assert await browser.evaluate("[...document.querySelectorAll('.project-nav-header')].every(e => e.checkVisibility())")
            await browser.shot(output, f"selected-beta-sidebar-phone-{width}")
            if width == 390:
                await browser.click_element(beta_group + ".querySelector('.project-assistant')")
                await browser.wait("[...document.querySelectorAll('.uber-pane')].some(e => e.checkVisibility() && e.getAttribute('aria-label').startsWith('Beta'))")
                await browser.wait("[...document.querySelectorAll('textarea')].some(e => e.checkVisibility() && !e.disabled)")
                await browser.wait(sidebar + ".getBoundingClientRect().right <= 0")
                await browser.wait("document.querySelector('.v-navigation-drawer--right').getBoundingClientRect().right <= innerWidth + 1")
                await browser.shot(output, "sidebar-chat-phone")
                await browser.click("Platform settings")
                await browser.wait("document.querySelector('h1')?.textContent === 'Platform settings'")
                await browser.wait("document.querySelector('.v-navigation-drawer--right').getBoundingClientRect().left >= innerWidth")
                await browser.shot(output, "global-settings-from-chat-phone")
                await browser.click("back to the fleet")
            elif await browser.evaluate(sidebar + ".classList.contains('v-navigation-drawer--active')"):
                await browser.click("show the run list")
        assert not browser.errors, browser.errors
        (output / "browser-results.json").write_text(json.dumps({
            "result": "pass", "viewports": ["1440x1000", "390x844", "320x844"],
            "checks": ["all sidebar projects while Beta selected; main view remains scoped", "empty project visible",
                       "real HTTP refresh updates other-project counts", "independent collapse retained through selection and refresh",
                       "native Enter/Space collapse and chat without toggling", "correct sidebar chat destination on desktop/phone",
                       "single reachable global settings cog opens settings on desktop/phone", "New project opens dialog",
                       "bar switcher shows the selected project and lists All projects, every project, its settings and New project",
                       "native Tab reveals the switcher tooltip; arrow keys reach every entry; keyboard and phone New project open dialog",
                       "All projects monitoring", "project colors and dark theme", "no phone overflow"],
            "browser_errors": browser.errors,
        }, indent=2))


if __name__ == "__main__":
    fixture.main(probe)
