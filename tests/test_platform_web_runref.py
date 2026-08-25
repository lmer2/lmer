"""Guards on clickable run references in chat (issue #241).

The parse and the dispatch registry are *executed* by
``tests/test_platform_web_runref.js``; what is read here is the wiring a DOM-less
test cannot execute, and the two constraints the issue set: the dispatch is
in-app (so the levers that would make a run view addressable are read for by
name), and a reference can only select a view.
"""

import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
RUNREF = WEB / "src" / "runref.js"
APP = WEB / "src" / "App.vue"
MARKDOWN = WEB / "src" / "components" / "Markdown.vue"
TASKDEF = (
    Path(__file__).resolve().parent.parent / "taskdef" / "orchestrate" / "instructions.txt"
)


def _read(path):
    return path.read_text(encoding="utf-8")


def test_the_parser_and_the_registry_behave():
    """Execute the module — the parse is this feature's security boundary."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    script = Path(__file__).resolve().parent / "test_platform_web_runref.js"
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "all assertions passed" in result.stdout


def test_the_renderer_reuses_the_module_s_shape():
    """A second copy is how a reference renders as a link the dispatcher then
    refuses to parse — a dead link with nothing on screen to say why."""
    text = _read(MARKDOWN)
    assert "from '../runref.js'" in text
    assert "RUN_REF_RE" in text
    assert "const RUN_REF_RE" not in text, (
        "Markdown.vue defines its own reference pattern — import it from runref.js"
    )
    # A regexp literal here would be that second copy under another name.
    assert "lmer:\\/\\/" not in text, (
        "Markdown.vue builds its own lmer: pattern — import RUN_REF_RE instead"
    )


def test_both_click_buttons_are_intercepted():
    """Both events, one handler — and only the middle button.

    `click` is the primary button only, so a middle click bypassed the handler
    entirely; `auxclick` is every non-primary button, so binding it without a
    filter made a right-click dispatch as well.
    """
    text = _read(MARKDOWN)
    assert '@click="onClick"' in text
    assert '@auxclick="onClick"' in text, (
        "a middle click bypasses a click-only handler and reaches the browser"
    )
    # The binding was right while the behaviour was wrong, which is what the
    # browser rig is for: this guard can see the string, not the dispatch.
    assert "event.button !== 1" in text, (
        "auxclick is bound without a button filter, so a right-click dispatches"
    )


def test_the_dispatch_is_not_navigation():
    """No router, no address bar. Read by name, because "we did not do that" is
    what a later convenience patch removes without noticing."""
    for path in (RUNREF, MARKDOWN):
        text = _read(path)
        for lever in ("window.location", "location.href", "history.pushState",
                      "useRouter", "router.push"):
            assert lever not in text, f"{path.name} navigates via {lever}"


def test_the_reference_carries_no_action():
    """Three key segments and the key: a field that could name a verb, a URL or a
    payload is what turns a view selector into a remote control."""
    lines = [
        line.strip() for line in _read(RUNREF).splitlines()
        if line.strip().startswith("return { host")
    ]
    assert lines == [
        "return { host, project, slug, key: `${host}/${project}/${slug}` }"
    ], lines


def test_the_shell_registers_a_resolver_and_reports_an_untracked_run():
    """App.vue owns tracked-vs-untracked because App.vue holds the fleet: the
    registration, and the notice that keeps an untracked key from being a dead
    link."""
    text = _read(APP)
    assert "onRunRef" in text and "stopRunRefs" in text, (
        "App.vue must register a run-reference resolver and drop it on unmount"
    )
    assert "runRefNotice" in text
    assert "does not track that run" in text
    # Against the fleet this shell already holds, not by a fetch of its own.
    resolver = text[text.index("function openRunRef"):]
    resolver = resolver[:resolver.index("\n}")]
    assert "runs.value.find" in resolver
    assert "open(run)" in resolver


def test_the_assistant_is_told_to_write_them():
    """A carrier nobody writes references into changes nothing (#241's "done
    when")."""
    text = _read(TASKDEF)
    assert "lmer://run/<host>/<project>/<slug>" in text
    # The two properties the note has to carry.
    assert "select a view" in text
    assert "Slack" in text
