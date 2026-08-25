"""The sanitiser's output, read in a real DOM (issue #381 item 5).

Its own module because :mod:`tests.test_platform_web_markdown` funnels every
executed check through one helper whose missing-toolchain answer is a *failure*;
this one needs a browser, which nothing declares a host should have, so it skips.

The rule it stands for: **an assertion about the sanitiser has to read what the
DOM ends up with.** In Node, ``DOMPurify.sanitize`` returns its input untouched,
so a Node assertion about it is really an assertion about markdown-it — which is
how ``target``/``rel`` came to be dropped in the browser while every test passed.
The property is pinned twice: a source guard that always runs, and the browser
probe that shows the declaration has the effect it is there for.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import node_binary, require_node_toolchain
from tests.test_platform_web_markdown import MARKDOWN, WEB, _read, _render_path


# This image happens to carry playwright's bundled chromium (the MCP server ships
# it). Overridable, because a path in a test is a guess about a host.
BROWSER_CANDIDATES = [
    "/home/developer/.npm-global/lib/node_modules/playwright/index.mjs",
    "/usr/lib/node_modules/playwright/index.mjs",
]

#: "No browser here", kept distinct from a failed assertion.
NO_BROWSER_EXIT = 3


def _dom_probe(body):
    """Run *body* with the real render path **and a real DOM** in scope.

    ``DOMPurify`` is loaded from the same ``node_modules`` the bundle is built
    from, and the config is the component's own object carried across as data
    (regexps by source, rebuilt in the page) rather than restated. Skips when
    there is no browser.
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    if not (WEB / "node_modules" / "dompurify").is_dir():
        require_node_toolchain("web dependencies are not installed (npm ci in web/)")

    script = "\n".join([
        "import MarkdownIt from 'markdown-it'",
        "import DOMPurify from 'dompurify'",
        "import assert from 'node:assert/strict'",
        "import { existsSync } from 'node:fs'",
        "import { pathToFileURL } from 'node:url'",
        "import { RUN_REF_RE } from './src/runref.js'",
        _render_path(),
        f"const candidates = [process.env.LMER_PLAYWRIGHT, {json.dumps(BROWSER_CANDIDATES)[1:-1]}]",
        "const entry = candidates.filter(Boolean).find((path) => existsSync(path))",
        f"if (!entry) {{ console.log('no browser'); process.exit({NO_BROWSER_EXIT}) }}",
        "const { chromium } = await import(pathToFileURL(entry).href)",
        # Attribute values are not JSON, and regexps are not either: carried by
        # source and rebuilt, so the page checks the component's own patterns.
        "const asData = (cfg) => JSON.parse(JSON.stringify(cfg, (key, value) => (",
        "  value instanceof RegExp ? { __re: value.source, __flags: value.flags } : value)))",
        "const browser = await chromium.launch({ headless: true })",
        "let sanitize",
        "try {",
        "  const page = await browser.newPage()",
        "  await page.goto('about:blank')",
        "  await page.addScriptTag({ path: 'node_modules/dompurify/dist/purify.js' })",
        "  sanitize = async (html, cfg) => page.evaluate(([raw, plain]) => {",
        "    const revive = (value) => (value && value.__re",
        "      ? new RegExp(value.__re, value.__flags)",
        "      : (Array.isArray(value) ? value.map(revive)",
        "        : (value && typeof value === 'object'",
        "          ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, revive(v)]))",
        "          : value)))",
        "    return window.DOMPurify.sanitize(raw, revive(plain))",
        "  }, [html, asData(cfg)])",
        body,
        "} finally {",
        "  await browser.close()",
        "}",
        "console.log('probe ok')",
    ])
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(WEB), capture_output=True, text=True, timeout=180,
    )
    if result.returncode == NO_BROWSER_EXIT:
        pytest.skip(
            "no browser to sanitise in (set LMER_PLAYWRIGHT to a playwright entry)"
        )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "probe ok" in result.stdout
    return result.stdout


def test_what_the_dom_ends_up_with_is_what_the_component_meant():
    """Three properties, the first two of which every Node assertion got wrong
    until ``ADD_URI_SAFE_ATTR``: an external link really does keep
    ``target``/``rel``, a reference keeps its href and gains neither, and hrefs
    are still URL-checked."""
    body = "\n".join([
        "const external = await sanitize(markdown.render('[x](https://ok.invalid/p)'), SANITIZE)",
        "assert.match(external, /target=\"_blank\"/, external)",
        "assert.match(external, /rel=\"noopener noreferrer\"/, external)",
        "const ref = 'lmer://run/gitlab.example.com/acme/widget/develop-issue-381'",
        "const reference = await sanitize(markdown.render(`[run](${ref})`), SANITIZE)",
        "assert.match(reference, new RegExp(`href=\"${ref}\"`), reference)",
        "assert.ok(!reference.includes('target='), `reference got a target: ${reference}`)",
        "assert.ok(!reference.includes('rel='), `reference got a rel: ${reference}`)",
        # The URI check itself is untouched by the two safe attributes.
        "for (const bad of ['javascript:alert(1)', 'data:text/html,x',",
        "                   'lmer://run/h/p/s?spawn=1']) {",
        "  const out = await sanitize(`<a href=\"${bad}\">x</a>`, SANITIZE)",
        "  assert.ok(!out.includes('href'), `${bad} survived sanitising: ${out}`)",
        "}",
        # Inline mode shares the config, so it must share the outcome.
        "const inline = await sanitize(markdown.renderInline('[x](https://ok.invalid/p)'), SANITIZE_INLINE)",
        "assert.match(inline, /target=\"_blank\"/, inline)",
    ])
    _dom_probe(body)


def test_the_sanitiser_declares_target_and_rel_uri_safe():
    """The half that still runs on a host with no browser — the case the defect
    shipped under."""
    text = _read(MARKDOWN)
    block = re.search(r"const SANITIZE = \{(.*?)\n\}", text, re.S)
    assert block, "Markdown.vue no longer has a SANITIZE config"
    safe = re.search(r"ADD_URI_SAFE_ATTR:\s*\[([^\]]*)\]", block.group(1))
    assert safe, (
        "the sanitiser does not declare any attribute URI-safe, so an allowed "
        "attribute whose value is not a URL (target, rel) is dropped in the browser"
    )
    named = {value.strip().strip("'\"") for value in safe.group(1).split(",")}
    assert {"target", "rel"} <= named, named
