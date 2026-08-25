"""Run references on their way to Slack (issue #241, raised in the !262 review).

Slack renders a CommonMark link as raw characters, so the degradation #241 wanted
is performed at the egress rather than assumed. The risk worth testing is not the
substitution but that **two implementations of one grammar now exist** — this one
and ``web/src/runref.js`` — so the corpus below is run through both and the
verdicts compared.
"""

import json
import subprocess
from pathlib import Path

from slack_chat.client import strip_run_references

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"

#: `(text, expected)`, shared because the cross-language check compares the same
#: cases both ways.
CASES = [
    # The shape the taskdef tells the assistant to write.
    (
        "[auth rate-limit fix](lmer://run/gitlab.example.com/acme/widget/develop-issue-412)"
        " is on its third round.",
        "auth rate-limit fix is on its third round.",
    ),
    # Several in one message, and a deeper project path.
    (
        "[a](lmer://run/h/p/s) and [b](lmer://run/gitlab.example.com/gh/acme/widget/r-9)",
        "a and b",
    ),
    # No label to fall back on: keep the key.
    ("[](lmer://run/h/p/s)", "h/p/s"),
    # Images are disabled in the renderer; the bang must not survive.
    ("![x](lmer://run/h/p/s)", "x"),
    # Not references — left exactly as written, because that is what they are.
    ("[label](lmer://run/h/p/s?spawn=1)", "[label](lmer://run/h/p/s?spawn=1)"),
    ("[label](lmer://run/h/s)", "[label](lmer://run/h/s)"),
    ("[label](lmer://spawn/h/p/s)", "[label](lmer://spawn/h/p/s)"),
    ("[label](lmerx://run/h/p/s)", "[label](lmerx://run/h/p/s)"),
    ("[label](lmer://run/h/p/s/../../etc)", "[label](lmer://run/h/p/s/../../etc)"),
    # `\[` is an escaped bracket: literal text, never a link.
    (r"\[literal](lmer://run/h/p/s)", r"\[literal](lmer://run/h/p/s)"),
    (
        r"escaped \[not a link](lmer://run/h/p/s) here",
        r"escaped \[not a link](lmer://run/h/p/s) here",
    ),
    # Fails safe rather than correct: a valid link left raw. Raw beats wrongly
    # rewritten, and the taskdef says to use the run's title.
    ("[outer [inner]](lmer://run/h/p/s)", "[outer [inner]](lmer://run/h/p/s)"),
    # An ordinary link is somebody else's problem (pre-existing).
    ("[label](https://example.invalid/x)", "[label](https://example.invalid/x)"),
    ("plain prose with no links at all", "plain prose with no links at all"),
    # A bare URL is not a link.
    ("bare lmer://run/h/p/s in prose", "bare lmer://run/h/p/s in prose"),
]


def test_references_are_reduced_to_their_labels():
    for text, expected in CASES:
        assert strip_run_references(text) == expected, text


def test_the_substitution_is_context_free_and_says_so():
    """A reference inside a code span is still reduced — deliberately, and the
    limitation is stated where a future reader will find it."""
    assert strip_run_references("`[x](lmer://run/h/p/s)`") == "`x`"
    doc = strip_run_references.__doc__
    assert "context-free" in doc
    assert "code span" in doc


def test_the_single_egress_converts():
    """At `post_message`, because converting per caller would leave the next
    poster added here a surface that leaks raw markdown."""
    from slack_chat.client import SlackClient

    # The sibling module's stub, so this says nothing about the transport.
    from tests.test_slack_client import _make_response

    client = SlackClient(bot_token="xoxb-test")
    sent = []

    def fake_request(method, url, **kwargs):
        sent.append((kwargs.get("json") or {}).get("text"))
        return _make_response({"ok": True, "ts": "1700000001.000100"})

    client.session.request = fake_request
    client.post_message(
        "C123", "1700000000.000001",
        "[auth fix](lmer://run/h/p/s) is done",
    )
    assert sent == ["auth fix is done"]


def test_the_two_implementations_agree_on_what_a_reference_is():
    """One grammar, two languages, one corpus — a copy that drifts is silent in
    both directions."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    # Per-href rather than inferred from the line it appeared in, so a mixed line
    # added later cannot skew the compare.
    hrefs = {}
    for text, _expected in CASES:
        for candidate in text.split("(")[1:]:
            href = candidate.split(")")[0]
            if href.startswith("lmer"):
                hrefs[href] = strip_run_references(f"[L]({href})") == "L"
    assert hrefs, "the corpus no longer contains any lmer: hrefs"

    script = "\n".join([
        "import { RUN_REF_RE } from './src/runref.js'",
        f"const hrefs = {json.dumps(sorted(hrefs))}",
        "console.log(JSON.stringify(hrefs.map((h) => RUN_REF_RE.test(h))))",
    ])
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(WEB), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    js_verdicts = dict(zip(sorted(hrefs), json.loads(result.stdout)))
    assert js_verdicts == hrefs, (
        "the Slack stripper and web/src/runref.js disagree about what a run "
        f"reference is: {js_verdicts} vs {hrefs}"
    )
