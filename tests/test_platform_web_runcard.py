"""Guards on the fleet view's row — the card the whole app is for (issue #141, T50).

The fleet view answers one question, "which run needs me", and ``RunCard.vue`` is
where it answers it. T50 asked the row to say considerably more: the taskdef, the
repo, the issue/PR/MR number, the preset, the fan-out selection and what is
driving the harness. Every one of those is useful, and adding them is also the
most direct way to destroy the thing the view exists for — a row of neutral chips
drowns the attention signal exactly as effectively as deleting it would, and the
result photographs beautifully.

So the guards here are about what the additions must not cost. Source-level, like
:mod:`tests.test_platform_web_app` and :mod:`tests.test_platform_web_shell`,
because this image has no browser; the two helpers that are pure logic
(``targetRef``, ``driverLabel``) are *executed* under Node instead of pinned by
reading, in the same shape :mod:`tests.test_platform_web_markdown` uses.

What each test keeps, and why losing it is silent:

- the state chip is still painted from ``toneColor()``, and the row still takes
  its tone from the *attention reason* rather than the state — the same pair
  ``test_the_compact_row_still_carries_the_state_tone`` keeps for the drawer;
- the signal keeps a line of its own. The longest state word in the vocabulary is
  ``detached — not being recorded``; put a project path on that flex line and the
  state wraps away from the name it belongs to, on a phone only;
- nothing that was *added* can be painted at all. Colour in this app is urgency
  (the ramp in main.js) plus one accent for "go and see this", and identity chips
  in the brand accent would compete with the alert for the eye on a fleet scanned
  at arm's length. The added chips are colourless, and this file fails if one of
  them gains a colour;
- and they are *outlined*, which is the one place this app allows the variant it
  swept out. The operator read a live fleet and could not read them ("quite hard to
  read, lets try variant outlined"): a tint sits behind mid-emphasis text, worst on
  a dark card. The state chip and the uber lmer badge stay tonal, so the signal is
  the only filled chip in the row and the badge stays ordinary — and no button gets
  to follow the chips out of the house style;
- the attention note stays above the launch chips: what a run needs is read before
  how it was launched;
- a launch fact the platform does not have renders *nothing* — never a blank chip
  and never "unknown". Which matters most for the model, because today the
  platform records none (see :mod:`lmer_platform.inventory`.RunView.model);
- a row is named by this orchestrator's *title* for the run when there is one, and
  by the run's own label when there is not (T65). Both listings — the fleet card
  and the drawer row — are pinned here together, because a fallback that drifts
  between them is a drawer you cannot match against the view beside it, and a
  listing that dropped the fallback would render an empty heading for every run
  nobody has described;
- the one row that IS the orchestrator says so, in both listings, from the flag the
  daemon puts on the row rather than from a taskdef the browser recognised (T85) —
  and every other row renders exactly as it did before that badge existed, which is
  what makes marking one row in a hundred safe. The marking itself has been through
  three passes with the operator: a badge, then a tinted row, and now an orange
  border on the fleet card plus the filled robot in place of the drawer row's state
  icon ("i don't think solid background for the uber lmer row is the way"). What the
  guards below keep is the shape of that: one *unpainted* badge in each listing, the
  border keyed on the flag and drawn in the theme's own accent, the drawer's glyph
  swapped in both directions, and the flag still read once per listing so what the
  marking reaches can be read off the file;
- both listings say where the work has got to, which they deliberately did not: the
  card buried the phase in its dimmed line and the drawer left it out. The operator
  asked for it, so it is rendered in each listing's own idiom — and absent renders
  nothing, which is a chat run and every run in its first seconds;
- the row says what the run says about *itself*, and says it quietly (T91). The
  state chip is derived, and liveness outranks the committed record in deriving it
  by design — so a live session's chip reads ``running`` while its own state.yaml
  can say something else, and a row showing only the chip makes that invisible.
  What is guarded is the subordination in both directions: the committed record is
  in the dimmed line rather than in a second chip, and it renders nothing at all on
  a run that has recorded nothing, which is every run in its first seconds;
- the row renders no markdown at all, and that is a decision rather than an
  omission (T46). This card is in the entry chunk — it *is* the landing screen —
  and one renderer in it fetches markdown-it and DOMPurify on the first paint of a
  list of runs, which is the 59 kB T42 took off that screen. The same attention
  note is rendered a tap later in the detail header;
- a target is a link only when it is an absolute URL. Rendered as an ``href``
  unconditionally, everything else a taskdef target can be — a branch, prose, the
  orchestrator's own ``fleet`` — became a *relative* link that 404'd against the
  page; the gate is one helper in ``format.js`` and is executed here in both
  directions, because a second copy of it in a component is how the row and the
  detail header start disagreeing;
- the ref in the row is the ticket number, parsed from the target with the same
  vocabulary ``lmer_cli.cli._derive_repo_url_from_task_target`` uses. That
  function cannot be imported into a browser, so its indicator list is read here
  and compared against the JS map: a resource kind added there arrives as a
  failing test rather than as a row that stops naming what it points at. And the
  parse stays clear of ``_parse_repo_url``, whose SSH branch is the
  character-class ``rstrip(".git")`` bug T28 found and T48 is fixing — a second
  copy of that grammar is how it comes back.

How any of it looks, whether three chips fit a 390px row, and whether the launch
line reads as quiet rather than as clutter, are verified by building the bundle
and by live test LT3 on a real phone.
"""

import inspect
import re
import subprocess
from pathlib import Path

# node_binary is the two-root Node lookup (T47): the pinned toolchain is
# invisible from inside the suite through the isolated platform dir, and a copy
# that forgot the second root would not fail, it would skip everywhere. In
# conftest because five modules want it, this one included.
from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
RUN_CARD = WEB / "src" / "components" / "RunCard.vue"
#: The shell, read only where a card hands it something to do (open-chat): the
#: card emits and the shell owns the drawer, and half of that contract lives here.
APP_VUE = WEB / "src" / "App.vue"
#: The drawer's row. Guarded here rather than beside the shell because the thing
#: under test is *how a listing names a run* — and the fleet card and the drawer
#: naming the same run differently would be a navigator you cannot match against
#: the view next to it (T65).
RUN_NAV = WEB / "src" / "components" / "RunNav.vue"
FORMAT = WEB / "src" / "format.js"
STYLE = WEB / "src" / "style.css"

#: What a listing renders where a run is named: this orchestrator's title when it
#: has one, and the run's own name when it does not. One spelling, asserted in both
#: listings, because a heading that can be empty is the failure mode — and a
#: fallback that drifts between the two views is the silent one.
NAME_EXPRESSION = "{{ run.title || run.label }}"

#: Every colour ``RunCard.vue`` is allowed to paint, what each one is for, and how
#: many things may wear it. Exhaustive rather than a ban on new ones, and counted
#: rather than merely listed: the point is that the row's palette is a decision, so
#: a chip that acquires a colour — or a second user of one that is already here —
#: has to be argued for in this table.
SANCTIONED_COLOURS = {
    # The signal itself, from the one place that decides what urgency looks like.
    ':color="toneColor(meta.tone)"': 1,
    # The brand accent, and two users: a published port is "go and see this", and
    # the orchestrator card's chat button is the same invitation for the one row
    # whose "this" is a conversation. The button matches the border the row already
    # wears (the scoped rule — not a colour prop, deliberately not counted here):
    # one accent, one meaning, drawn twice on the one card that is the accent's own.
    # The uber lmer *badge* stopped wearing it on the operator's second pass — in a
    # listing the accent already means "you are here", and a badge is not a control.
    'color="primary"': 2,
    # The answer button, which is only ever on a row that is already amber.
    'color="warning"': 1,
}

#: Every ``<v-chip`` the row draws, keyed by the attribute that says which chip it
#: is, and the variant it wears. Exhaustive and anchored per chip like the colour
#: table above, because the chips repeat and "one of them is tonal" is not something
#: a substring search can tell you.
#:
#: The outlined ones are the operator's call from a live pass: "the chips (questions
#: answers, port links) are quite hard to read, lets try variant outlined". A tonal
#: chip is a low-contrast wash behind mid-emphasis text — worst on a dark card, and
#: this row is read at arm's length — while outlined gives the word full-strength
#: colour on the card's own surface. That overrides, for chips only, the variant that
#: was swept out of the app; every ``v-btn`` in this row stays tonal, and the guard
#: below checks that rather than only the presence of a string.
#:
#: The two that stay tonal are the two that are not information:
#:
#: - the state chip is the signalling system, and the tint is now the one *filled*
#:   thing in the row — which is exactly why it keeps winning the glance. Outlined it
#:   would be a fifth coloured word in a border;
#: - the uber lmer badge is deliberately ordinary (unpainted, third pass with the
#:   operator: the row's border carries that identity), and an outlined chip with no
#:   colour is a grey ring saying nothing the word does not.
CHIP_VARIANTS = {
    ':color="toneColor(meta.tone)"': "tonal",
    'v-if="orchestrator"': "tonal",
    'v-if="run.target"': "outlined",
    'v-if="run.preset"': "outlined",
    'v-if="run.agents"': "outlined",
    'v-if="driver"': "outlined",
    'v-for="port in run.ports"': "outlined",
}


def _read(path):
    return path.read_text(encoding="utf-8")


def _template():
    """``RunCard.vue``'s template, without the script block.

    Several assertions below are about *order* — which of two things the eye
    reaches first — and the script block mentions most of them in a different one.
    """
    text = _read(RUN_CARD)
    return text[text.index("<template>"):]


def _script():
    text = _read(RUN_CARD)
    return text[:text.index("</script>")]


def _without_comments(text):
    """*text* with its markup and line comments removed.

    For the assertions that are about what the code *does*: these components explain
    themselves at length, and a token in a comment reaches nothing.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def _first_row():
    """The card's first row: the state chip and the run's name.

    Sliced from its opening ``<div`` to the next closing tag, which is exact here
    because no row in this card nests another.
    """
    template = _template()
    start = template.index('<div class="d-flex')
    return template[start:template.index("</div>", start)]


def _quiet_row():
    """The card's dimmed line: what the run says about itself, its plan, its age.

    Sliced the way :func:`_first_row` is, and exact for the same reason — nothing in
    this line nests a second ``div``. Which is itself part of what is under test: the
    line is a line, so everything in it is one text size and one emphasis.
    """
    template = _template()
    start = template.index('<div class="d-flex flex-wrap ga-3 text-body-small')
    return template[start:template.index("</div>", start)]


def _style_rule(text, selector):
    """One declaration block from a component's scoped stylesheet.

    Same shape as the helper in :mod:`tests.test_platform_web_app`, for the same
    reason: the assertion is about what one rule declares, so a missing property
    reads as a missing property — and it takes the source rather than reading
    ``RunCard.vue`` itself, because the drawer's row is guarded here too and two
    copies of this is how one of them ends up reading the wrong file.
    """
    style = text[text.index("<style"):]
    assert f"{selector} {{" in style, f"no {selector} rule in this component"
    block = style[style.index(f"{selector} {{"):]
    return block[:block.index("}")]


def _badge(text):
    """The ``uber lmer`` chip's own markup, from either listing.

    Sliced back from the word rather than forward from a ``<v-chip``: both files draw
    several chips and this is the one identified by what it says, which is also the
    only thing about it that has survived three passes of the operator's.
    """
    end = text.index(">uber lmer<")
    return text[text.rindex("<v-chip", 0, end):end]


def _function_body(text, signature):
    """Source of one top-level function in a JS module.

    Same helper as :mod:`tests.test_platform_web_shell`: every function in
    ``format.js`` is top-level, so a ``}`` in column zero ends one.
    """
    start = text.index(signature)
    return text[start:text.index("\n}\n", start)]


def _binding(name):
    """Source of one top-level binding in ``RunCard.vue``'s ``<script setup>``.

    From its ``const`` to whatever starts at column zero after it. Not
    :func:`_function_body`: these are arrow bodies, so they end in ``)`` or ``})``
    rather than in a brace of their own, and the assertions below are about what
    is *inside* one — a lookup that moved, a fallback that changed.
    """
    script = _script()
    rest = script[script.index(f"const {name} = "):]
    match = re.search(r"\n(?=(?:const|function|//|/\*)\s)", rest)
    return rest[:match.start()] if match else rest


def _state_tones():
    """``format.js``'s state → tone mapping, parsed out of STATE_META."""
    block = re.search(r"const STATE_META = \{(.*?)\n\}", _read(FORMAT), re.S)
    assert block, "format.js no longer maps states to tones"
    return dict(re.findall(r"(\w+): \{[^}]*tone: '(\w+)'", block.group(1)))


def _state_labels():
    """``format.js``'s state → label mapping, parsed out of STATE_META."""
    block = re.search(r"const STATE_META = \{(.*?)\n\}", _read(FORMAT), re.S)
    assert block, "format.js no longer labels states"
    return dict(re.findall(r"(\w+): \{ label: '([^']*)'", block.group(1)))


def _target_kinds():
    """``format.js``'s path-segment → resource map, as ``{segment: body}``.

    ``body`` is the mapped ``{ kind, word }`` literal, or ``None`` for a segment
    the grammar deliberately does not treat as a ref.
    """
    block = re.search(r"const TARGET_KINDS = \{(.*?)\n\}", _read(FORMAT), re.S)
    assert block, "format.js no longer maps a target's path onto a resource"
    kinds = {}
    for name, body in re.findall(r"^  (\w+): (.+),$", block.group(1), re.M):
        kinds[name] = None if body == "null" else body
    assert kinds, "TARGET_KINDS maps nothing"
    return kinds


def _kind_names():
    """The ``kind`` values ``targetRef`` can return."""
    return {
        re.search(r"kind: '(\w+)'", body).group(1)
        for body in _target_kinds().values() if body
    }


def _resource_icons():
    """``RunCard.vue``'s resource-kind → ``@mdi/js`` icon map.

    The values are asserted to be ``mdi*`` identifiers, not path data: a pasted
    path drifts from the package and defeats the tree-shaking that keeps this
    bundle's icon set to the handful it uses.
    """
    block = re.search(r"const RESOURCE_ICONS = \{(.*?)\n\}", _read(RUN_CARD), re.S)
    assert block, "RunCard.vue no longer draws the kind of thing a run is against"
    icons = dict(re.findall(r"(\w+): (mdi\w+),", block.group(1)))
    assert icons, "RESOURCE_ICONS maps nothing to an @mdi/js icon"
    return icons


def _cli_indicators():
    """The resource paths ``lmer_cli``'s target parser recognises, from its source.

    Read off the imported function rather than the file, so the comparison is
    against the grammar that actually ships.
    """
    from lmer_cli.cli import _derive_repo_url_from_task_target

    source = inspect.getsource(_derive_repo_url_from_task_target)
    block = re.search(r"indicators = \((.*?)\)", source, re.S)
    assert block, "the CLI's target grammar no longer lists the paths it accepts"
    found = {token.strip("/") for token in re.findall(r"'([^']+)'", block.group(1))}
    assert found, "the CLI's indicator list is empty"
    return found


def _probe(body):
    """Run *body* under Node with ``format.js``'s real helpers in scope.

    The module is imported by URL because it has no dependencies of its own — the
    same property that lets ``tests/test_platform_web_format.js`` run it — so this
    needs no ``node_modules`` and no build step.

    Whether a missing toolchain skips or fails is :func:`require_node_toolchain`'s
    decision (T47): on a host that says it has Node, the absence is a failure,
    because a skip takes the only executed coverage in this file with it and
    leaves the run green.
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    script = "\n".join([
        "import assert from 'node:assert/strict'",
        "import { checkinLabel, driverLabel, duration, targetLink, targetRef } "
        f"from {FORMAT.as_uri()!r}",
        body,
        "console.log('probe ok')",
    ])
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "probe ok" in result.stdout
    return result.stdout


# --- the signal survives the additions ---------------------------------------

def test_the_row_still_leads_with_the_state_tone():
    """The one thing the row must not trade for information.

    Same property ``test_the_compact_row_still_carries_the_state_tone`` keeps for
    the drawer, one view over: the tone comes from ``toneColor()`` rather than
    from a colour this component picked, and the row's tone is the *attention
    reason's* — a live question sits on a session that is ``running``, and a calm
    green chip at the top of "needs you" contradicts the only thing that row is
    there to say.
    """
    text = _read(RUN_CARD)
    assert ':color="toneColor(meta.tone)"' in text, (
        "the state chip is not painted from the one place that decides what "
        "urgency looks like"
    )
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", text), (
        "a colour hardcoded in a component outlives the theme that matched it"
    )
    assert "tone ? ['tone-edge', `tone-edge-${tone}`] : null" in text, (
        "the stripe down the card's edge is gone, or it is keyed on the state "
        "instead of the row — and a live question sits on a session that is "
        "running, so the state would paint that card calm"
    )

    tone = _binding("tone")
    assert "if (!props.run.attention) return null" in tone, (
        "a run with nothing waiting on a human still gets a stripe, which is how "
        "a stripe on every row ends up meaning nothing"
    )
    assert "run.attention.reason" in tone, (
        "the stripe is no longer keyed on why a human is needed"
    )
    assert "'bad' : 'attention'" in tone, "a crash and a question read the same"


def test_the_signal_shares_its_line_with_nothing_that_can_wrap_it_away():
    """The state word is the long one, and flex lines wrap.

    ``detached — not being recorded`` next to a run name already fills a phone's
    width; add a taskdef, a project path and a ticket chip to the same flex row
    and the state chip wraps to a second line, away from the name it qualifies —
    or off the fold entirely. Invisible on a desktop, which is where it would be
    written.
    """
    row = _first_row()
    assert "meta.label" in row and "run.label" in row, (
        "the card's first row is no longer the state and the name"
    )
    for added in ("run.taskdef", "run.project", "run.target"):
        assert added not in row, (
            f"{added} shares the signal's flex line, so a long value can wrap the "
            "state chip away from the name it belongs to"
        )


def test_nothing_the_row_gained_is_painted_with_a_tone_or_the_accent():
    """Colour is this view's signalling system; identity does not get to use it.

    main.js re-separated the whole ramp around deep orange so the brand accent
    could not be read as "this needs you". A row that paints its taskdef, its
    ticket or its preset chip undoes that arrangement from the other side: the
    alert is still there, still amber, and no longer the loudest thing in the row.

    An exhaustive table, so a colour added anywhere in this card — or a second
    thing wearing one that is already here — has to be argued for in
    :data:`SANCTIONED_COLOURS` rather than merely looking nice.
    """
    text = _read(RUN_CARD)
    found = re.findall(r':?color="[^"]+"', text)
    assert set(found) <= set(SANCTIONED_COLOURS), (
        "RunCard.vue paints something new: "
        f"{sorted(set(found) - set(SANCTIONED_COLOURS))}"
    )
    for colour, allowed in SANCTIONED_COLOURS.items():
        assert found.count(colour) == allowed, (
            f"{colour} is painted {found.count(colour)} times rather than "
            f"{allowed}; each of these is one specific meaning, and a new user of "
            "one is a claim that it is the same meaning again"
        )
    # The other way to paint something, and the one the check above would miss:
    # Vuetify's colour utilities tint an element with no `color` prop in sight.
    tinted = re.findall(
        r"\b(?:text|bg)-(?:primary|secondary|success|warning|error|info|tone-\w+)\b",
        text,
    )
    assert not tinted, (
        f"RunCard.vue tints something with a utility class: {sorted(set(tinted))}"
    )


def test_the_facts_are_outlined_chips_and_the_signal_is_the_filled_one():
    """What the operator asked for after reading a live fleet, and what it cost.

    "the chips (questions answers, port links) are quite hard to read, lets try
    variant outlined" — so the information chips take the variant this app had swept
    out, for the reason it was swept out in the first place read the other way: a
    tonal chip is colour *behind* the words, and mid-emphasis text on a tint is a
    label you resolve rather than read.

    The pins are per chip (:data:`CHIP_VARIANTS`) rather than a count, because the
    interesting failure is one chip drifting back — and two of them are meant to be
    tonal, which no file-wide search can express. The exception is also bounded here:
    ``outlined`` is allowed on a ``<v-chip`` and on nothing else in this row, so the
    buttons cannot follow the chips out of the house style.
    """
    text = _read(RUN_CARD)

    tags = re.findall(r"<v-chip\b[^>]*>", text)
    assert len(tags) == len(CHIP_VARIANTS), (
        f"the row draws {len(tags)} chips and {len(CHIP_VARIANTS)} are argued for; "
        "a new one has to say in that table which of the two it is"
    )
    for tag in tags:
        anchors = [anchor for anchor in CHIP_VARIANTS if anchor in tag]
        assert len(anchors) == 1, (
            f"this chip matches {anchors or 'no'} entry of the variant table: {tag}"
        )
        variant = CHIP_VARIANTS[anchors[0]]
        assert f'variant="{variant}"' in tag, (
            f"the chip anchored on {anchors[0]} is no longer {variant}, which is "
            "either an unreadable fact or a signal that stopped leading the row"
        )

    misplaced = {
        tag for tag in re.findall(r"<([\w-]+)\b[^>]*variant=\"outlined\"", text)
        if tag != "v-chip"
    }
    assert not misplaced, (
        f"variant=\"outlined\" reached {sorted(misplaced)}; the chips are the one "
        "place it is allowed back"
    )
    assert 'variant="flat"' not in text, (
        "flat is swept out of this app and stays swept out"
    )


def test_what_a_run_needs_is_still_read_before_how_it_was_launched():
    """Order is the other half of prominence, and it is invisible in a diff.

    The attention note is the reason a row is at the top of the fleet. Put the
    launch chips above it and the row leads with configuration; the note is still
    rendered, still amber, and now below three grey chips on a phone.
    """
    template = _template()
    assert template.index("<v-alert") < template.index('v-if="launched"'), (
        "the launch chips sit above the note that says what the run needs"
    )
    assert template.index('v-if="launched"') < template.index(">enter<"), (
        "the buttons are no longer the end of the card"
    )
    assert "attentionLabel(run.attention.reason)" in template
    assert "run.attention.note" in template, "the note is no longer shown"
    assert "text-truncate" not in template, (
        "something in this row truncates with Vuetify's one-line ellipsis. The "
        "note is bounded by a clamp of its own now (see the test below) and the "
        "label above it is the one thing here that must never be cut"
    )


def test_a_detached_session_never_reads_as_a_healthy_running_one():
    """``detached`` exists to stop a UI implying liveness nobody verified.

    The process is up, but the platform lost its only view of it and nothing is
    being recorded. The row keeps that apart from ``running`` in two channels, and
    both are one refactor from gone: the colour (``format.js`` separates the
    tones) and the words (it separates the labels, and the card renders the label
    verbatim instead of restating a state vocabulary of its own).
    """
    tones = _state_tones()
    assert tones["detached"] != tones["running"], (
        "format.js gives a detached session a healthy run's tone"
    )

    labels = _state_labels()
    assert labels["detached"] != labels["running"], (
        "format.js gives a detached session a healthy run's word"
    )
    assert labels["detached"] != "detached", (
        "the row says only 'detached', which reads as a state rather than as the "
        "warning it is — nothing is recording this session"
    )

    template = _template()
    assert "{{ meta.label }}" in template, (
        "the chip no longer renders format.js's label, so the state vocabulary "
        "has a second home that can disagree with the first"
    )
    script = _script()
    assert "stateMeta(props.run.state)" in script
    # Every read of the raw state, by the spelling that reads it. Two, and each has
    # to be one of these: the state handed to format.js, and the membership test
    # against the named table of ended states that decides whether the row may
    # offer to stop tracking the run (T101 — the table is pinned against
    # inventory.RUN_STATES by its own test). What this keeps out is the third kind
    # of read: a comparison against a state spelled in this component, which is how
    # a row grows a vocabulary that can disagree with format.js's about what
    # `detached` means.
    reads = ("stateMeta(props.run.state)", "ENDED_STATES.includes(props.run.state)")
    assert script.count("run.state") == len(reads), (
        f"the raw state is read {script.count('run.state')} times; each read has "
        f"to be one of {reads}"
    )
    for read in reads:
        assert read in script, f"the row no longer reads the state as {read}"
    assert not re.search(r"run\.state\s*(===|!==|==|!=)", script), (
        "the row compares the state against a literal of its own rather than "
        "reading it through format.js or the table above"
    )


# --- what the row now says ---------------------------------------------------

def test_the_row_says_what_the_run_is_working_on():
    """T50's actual ask: the taskdef, the repo and the ticket, in the row.

    Without these, deciding which of nine ``develop`` runs is the one you care
    about means opening them. Each is checked with its icon, because the icon is
    what says *which* field a bare value like ``develop`` or ``agents/global`` is.
    """
    row = _template()
    identity = row[row.index('<div class="d-flex flex-wrap align-center ga-3 mt-2">'):]
    identity = identity[:identity.index("<div", 1)]

    assert "{{ run.taskdef }}" in identity, "the row does not name the taskdef"
    assert "{{ run.project }}" in identity, "the row does not name the repo"
    assert "run.host" in identity, (
        "the host is nowhere in the row, so two same-named projects on two hosts "
        "are indistinguishable"
    )
    assert "resource ? resource.label : shortTarget(run.target)" in identity, (
        "the ticket number is not shown, or its fallback for a target that names "
        "no resource is gone — which would leave the row with no link at all"
    )
    # One per bare value on this line — the taskdef, the repo, and (since the
    # operator asked for it) the phase. Counted rather than merely present, because
    # the failure this catches is a value added here with nothing saying what it is.
    assert identity.count(':icon="mdi') == 3, (
        "the taskdef, the repo and the phase are bare values; the icon is what says "
        "which field each one is"
    )
    assert ':prepend-icon="resourceIcon"' in identity, (
        "the ticket chip does not draw what kind of thing it points at"
    )
    assert 'rel="noopener"' in identity, "the outbound link lost rel=noopener"


def test_the_three_kinds_of_target_draw_three_different_shapes():
    """An MR run is not an issue run, and shape is the channel that survives a glance.

    Same argument RunNav's ``STATE_ICONS`` makes for states: two things that mean
    different things must not draw the same glyph, or the only thing keeping them
    apart is a word you have to read. Tied to the grammar rather than restated, so
    a resource kind added to ``format.js`` cannot arrive with no shape.
    """
    icons = _resource_icons()
    assert set(icons) == _kind_names(), (
        "RunCard draws icons for a different set of kinds than targetRef can "
        f"return: {sorted(set(icons))} vs {sorted(_kind_names())}"
    )
    assert len(set(icons.values())) == len(icons), (
        f"two kinds of target draw the same shape: {icons}"
    )
    fallback = re.search(r": (mdi\w+)\)", _binding("resourceIcon"))
    assert fallback, (
        "a target that names no resource draws no icon at all, which is a chip "
        "with a hole where every other row has a shape"
    )
    assert fallback.group(1) not in icons.values(), (
        "a target that names no resource borrows a kind's shape, so the row "
        "claims to know something it does not"
    )


def test_a_launch_fact_the_platform_lacks_is_absent_rather_than_blank():
    """Three chips that are usually not there at all.

    Preset, fan-out and the driving model are recorded on the spawn entry, which
    means most rows have none of them — and the model has none *today* on every
    row, because nothing records it yet (T51). A chip that renders "unknown", or
    an empty line held open for one, is a permanent cost paid for information
    that is usually absent.
    """
    text = _read(RUN_CARD)
    for binding in ('v-if="run.preset"', 'v-if="run.agents"', 'v-if="driver"'):
        assert text.count(binding) == 1, (
            f"{binding} is not what decides whether that chip is drawn"
        )
    assert 'v-if="launched"' in text, (
        "the launch line is always in the layout, so a run that recorded none of "
        "it still pays for the row"
    )
    launched = _binding("launched")
    for value in ("run.preset", "run.agents", "driver.value"):
        assert value in launched, f"the launch line ignores {value}"

    for placeholder in ("'—'", '"—"', "'unknown'", "'default'", "'none'"):
        assert placeholder not in text, (
            f"the row renders {placeholder} where a value is missing; a chip that "
            "says nothing is known is worse than no chip"
        )


def test_the_way_into_a_run_is_called_enter():
    """The operator's word, and a plain one: the button opens the run, it does not
    describe it.

    Cheap to check and cheap to lose in a refactor that reinstates the old label
    from muscle memory.
    """
    template = _template()
    assert ">enter<" in template, "the row's way in is not called 'enter'"
    assert ">details<" not in template


# --- what a listing calls a run (T65) ----------------------------------------

def test_both_listings_name_a_run_by_its_title_and_fall_back_to_its_label():
    """The operator's ask, and the reason it is one expression in two files.

    "if meta.title is set it should be used in the details header and the
    listings". A title that is only visible inside its own tab cannot be what an
    operator identifies a run by, which is the whole point of storing one (T52:
    "meant for the user to be able to quickly identify what a run is about").

    The fallback is the load-bearing half: most runs have no title, and ``label``
    is the run's own name — its recorded name, else its slug — so it is always
    there. A listing that showed ``run.title`` alone would render *blank rows* for
    every run nobody has described, and it would look perfectly fine on the one
    run the author titled while testing it.
    """
    for source in (RUN_CARD, RUN_NAV):
        text = _read(source)
        assert text.count(NAME_EXPRESSION) == 1, (
            f"{source.name} does not name a run by {NAME_EXPRESSION} exactly once "
            "— the two listings have to agree, and the fallback is what stops a "
            "row that nobody has described rendering an empty heading"
        )
        assert "run.label" in text, f"{source.name} lost the run's own name entirely"


def test_a_title_in_a_row_is_text_rather_than_markdown():
    """Decided, not defaulted: a title is a label and stays one.

    It is agent-written text, so the question is real — but it arrives collapsed to
    one line and bounded at 120 characters by the daemon, and a row cannot host a
    heading, a list or a fenced block. ``RunMeta.vue`` made the same call about the
    same string in the tab that owns it, and the app has exactly one component
    allowed to turn text into markup. Vue interpolation escapes.

    The inline mode exists now (T46), which makes this decision *easier* to undo by
    accident rather than harder: rendering a title compactly no longer breaks the
    row's layout, it just puts a bold half-sentence where a name goes. And in this
    file the renderer costs more than appearance — see the test below.
    """
    for source in (RUN_CARD, RUN_NAV):
        text = _read(source)
        for rendering in ("<Markdown", "v-html", "innerHTML"):
            assert rendering not in text, (
                f"{source.name} renders markup ({rendering}); a run title is a "
                "label, and a row is not a document"
            )


def test_the_rows_one_piece_of_agent_prose_costs_the_landing_screen_nothing():
    """Why the attention note is shown as text while everything else renders (T46).

    The note *is* prose an agent wrote, and every other view of it renders it. The
    fleet row does not, because this card is in the entry chunk: it is what a phone
    opens on every glance, and a renderer here — lazily imported, inline, however
    carefully — is markdown-it and DOMPurify fetched on the first paint of a list
    of thirty rows, to italicise a clause in one dimmed sentence. That is the
    saving T42 exists for, given back for appearance.

    What is *not* given up: the note itself, whole and untruncated, and the
    rendered version one tap away in the detail header. Both halves are pinned,
    because the decision is only defensible with the second one.
    """
    text = _read(RUN_CARD)
    assert "{{ run.attention.note }}" in text, (
        "the row stopped showing the note; unrendered is not the same as dropped"
    )
    assert "import('./Markdown.vue')" not in text, (
        "the fleet row pulls in the renderer chunk, so the landing screen pays for "
        "a renderer it has nothing to render"
    )
    # The argument lives beside the note, where the next person to reach for a
    # renderer will be standing.
    assert "T46" in text and "landing screen" in text, (
        "nothing in the card says why this note is the one piece of agent prose "
        "in the app that is shown as characters"
    )
    detail = _read(WEB / "src" / "components" / "RunDetail.vue")
    assert re.search(
        r'<Markdown\s+:text="run\.attention\.note"\s+inline\s*/>', detail,
    ), (
        "the detail view does not render the note either, so the row's decision "
        "costs the operator the rendering entirely rather than deferring it"
    )


def test_a_long_title_wraps_under_the_state_chip_instead_of_pushing_it_off():
    """120 characters of agent-written text on a 390px row.

    Two things keep that from costing the signal. The chip is *before* the name on
    the flex line, so a long title wraps under it rather than shoving it out of
    view — the state is the one thing this row must not lose. And the name wears
    ``.scroll-x``, because a title can be one unbreakable string (a path, a URL, a
    branch name): it wraps when it can, and scrolls inside its own box when it
    cannot, instead of taking the page sideways on a phone.
    """
    row = _first_row()
    assert row.index("toneColor(meta.tone)") < row.index(NAME_EXPRESSION), (
        "the run's name comes before the state chip, so a long title pushes the "
        "signal off the row"
    )
    name = re.search(r"<span[^>]*>\{\{ run\.title \|\| run\.label \}\}", row, re.S)
    assert name and "scroll-x" in name.group(0), (
        "the name in the fleet row does not scroll inside its own box, so an "
        "unbreakable title takes the whole page sideways with it"
    )
    rule = re.search(r"\.scroll-x \{(.*?)\}", _read(STYLE), re.S)
    assert rule, "style.css lost the .scroll-x rule the row depends on"
    for declaration in ("overflow-x: auto", "min-width: 0"):
        assert declaration in rule.group(1), (
            f".scroll-x no longer declares {declaration}, which is what keeps a "
            "long title inside its own box"
        )


def test_the_drawers_truncated_title_is_still_readable_somewhere():
    """The drawer is the one listing that *cuts* the name.

    Vuetify ellipsises a list-item title, which is the right answer in a 300px
    drawer — a wrapped 120-character title would be a five-line row. It is the
    wrong answer to leave with no way to read the rest, so the whole string is the
    row's tooltip.
    """
    nav = _read(RUN_NAV)
    assert '<v-list-item-title :title="run.title || run.label">' in nav, (
        "the drawer row cuts the name with an ellipsis and offers no way to read "
        "the whole of it"
    )


# --- one grammar for a target ------------------------------------------------

def test_the_target_grammar_knows_every_resource_the_cli_derives_from():
    """The row and the CLI read the same URLs, and only one of them is Python.

    ``_derive_repo_url_from_task_target`` reads a task target for the *repo*; the
    row reads it for the *number*. Same GitHub PR and issue URLs, same GitLab MR,
    ``issues`` and ``work_items`` forms, same subgroup paths. It cannot be
    imported into a browser, so this is the seam: its indicator list is the
    vocabulary, and each entry must be classified in ``TARGET_KINDS`` — either as
    a resource with a word, or explicitly as ``null`` for a list page or a diff
    range that has no single number to name.

    A kind added on either side therefore arrives as a failing test, and somebody
    decides which bucket it is in, instead of a row quietly stopping naming what
    it points at.
    """
    kinds = _target_kinds()
    indicators = _cli_indicators()

    for indicator in indicators:
        assert indicator in kinds, (
            f"lmer_cli derives a repo from {indicator!r} targets and format.js "
            "has no verdict on them — the row would call such a target unnamed"
        )
    for segment in kinds:
        assert segment in indicators, (
            f"format.js reads {segment!r} out of a target that lmer_cli does not "
            "recognise as a resource path at all; one of the two is wrong"
        )
    # The three the row actually names, spelled out: a map that classified
    # everything as null would satisfy the two loops above and name nothing.
    assert _kind_names() == {"issue", "mr", "pr"}, (
        f"the row names {sorted(_kind_names())}, not an issue, an MR and a PR"
    )


def test_the_ref_is_read_from_path_segments_and_not_from_a_repo_url_parser():
    """No second copy of the repo-URL grammar, and T28's bug is the reason.

    ``_parse_repo_url``'s SSH branch strips ``.git`` with a character-class
    ``rstrip``, which turns ``group/project`` into ``group/projec`` (T28 found it,
    T48 is fixing it). Reading a number out of a target needs none of that
    machinery — path segments and a digit check — and a helper here that grew a
    suffix strip or an ``scp``-style host split would be that bug's second home.
    """
    body = _function_body(_read(FORMAT), "export function targetRef(target)")
    assert "new URL(target).pathname" in body, (
        "the ref is no longer read from the URL's path"
    )
    assert "split('/')" in body, "the path is matched as text, not as segments"
    assert ".git" not in body, "the ref parser grew repo-URL suffix surgery"
    assert "git@" not in body, "the ref parser grew a second repo-URL grammar"
    assert "catch" in body, (
        "a target that is not a URL — plenty of taskdefs take prose — throws out "
        "of the row instead of falling back to the shortened target"
    )


# --- executed: the two helpers that are pure logic ---------------------------

def test_a_target_names_the_issue_pr_or_mr_it_points_at():
    """The parse itself, run rather than read.

    Every shape the CLI's grammar covers, including the two that are easy to get
    wrong: a GitLab subgroup path (the resource is after ``/-/``, however many
    segments precede it) and ``work_items``, which is a newer URL form for an
    *issue* and must not read as a fourth kind of thing.
    """
    _probe("""
    assert.deepEqual(
      targetRef('https://gitlab.example.com/agents/global/-/merge_requests/164'),
      { kind: 'mr', number: '164', label: 'MR #164' },
    )
    assert.deepEqual(
      targetRef('https://gitlab.example.com/a/b/c/d/-/merge_requests/7'),
      { kind: 'mr', number: '7', label: 'MR #7' },
      'a subgroup path pushed the resource out of reach',
    )
    assert.deepEqual(
      targetRef('https://gitlab.example.com/agents/global/-/issues/93'),
      { kind: 'issue', number: '93', label: 'issue #93' },
    )
    assert.deepEqual(
      targetRef('https://gitlab.example.com/agents/global/-/work_items/141'),
      { kind: 'issue', number: '141', label: 'issue #141' },
      'work_items is GitLab spelling an issue differently, not a new kind',
    )
    assert.deepEqual(
      targetRef('https://github.com/owner/repo/pull/12'),
      { kind: 'pr', number: '12', label: 'PR #12' },
    )
    assert.deepEqual(
      targetRef('https://github.com/owner/repo/issues/34'),
      { kind: 'issue', number: '34', label: 'issue #34' },
    )
    // A deep link is still that resource.
    assert.equal(targetRef('https://github.com/owner/repo/pull/12/files').number, '12')
    """)


def test_a_target_that_names_no_numbered_resource_says_so():
    """Null is the answer, and the row shows the shortened target instead.

    A repo with no ticket, a compare range, a list page, a taskdef whose target is
    prose — all ordinary. The failure mode being guarded is the opposite of a
    crash: a grammar loose enough to invent a number from a path that has none, so
    a row confidently says "issue #2" about a two-segment repo path.
    """
    _probe("""
    for (const target of [
      null,
      '',
      'issue-141',
      'https://gitlab.example.com/agents/global',
      'https://gitlab.example.com/agents/global.git',
      'https://github.com/owner/repo',
      'https://github.com/owner/repo/pulls',
      'https://github.com/owner/repo/compare/main...topic',
      'https://github.com/owner/repo/commits/main',
      'https://github.com/owner/repo/pull/not-a-number',
      'https://github.com/owner/repo/pull',
      'https://gitlab.example.com/agents/global/-/merge_requests',
      'https://gitlab.example.com/work_items/141',
      'not a url at all',
    ]) {
      assert.equal(targetRef(target), null, `invented a resource in ${target}`)
    }
    """)


def test_the_driver_chip_stays_silent_until_something_is_actually_known():
    """What is driving the harness — and today, mostly, nobody here knows.

    ``harness`` is on the spawn entry only when the request named one; ``model``
    is on nothing at all yet (T51). So the interesting case is not "both known",
    it is "neither" — which has to come back null, because a chip reading
    ``unknown`` where a model belongs is a row asserting the platform looked.
    """
    _probe("""
    assert.equal(driverLabel({}), null, 'a run with nothing recorded got a chip')
    assert.equal(driverLabel({ harness: null, model: null }), null)
    assert.equal(driverLabel({ harness: '', model: '' }), null)
    assert.equal(driverLabel({ harness: 'codex' }), 'codex')
    assert.equal(driverLabel({ model: 'gpt-5.6-sol' }), 'gpt-5.6-sol')
    assert.equal(
      driverLabel({ harness: 'codex', model: 'gpt-5.6-sol' }),
      'codex · gpt-5.6-sol',
    )
    """)


# --- the row that IS the orchestrator (T85) ----------------------------------

def test_both_listings_mark_the_platforms_own_session():
    """The operator asked: "the orchestrator run needs to be clearly marked that it
    is that".

    One row in the fleet is the session that spawns, answers and stops the others,
    and read as an ordinary run it is the row an operator winds down by mistake. So
    both listings badge it, in the same words the drawer uses — "uber lmer", never
    "assistant", which is the label decision T31 made and the code deliberately does
    not follow — and with the drawer's icon, because it is the same thing being named
    in two places.

    Unpainted in both, which is the operator's third pass talking: the badge wore the
    row's own colour while the row was tinted, and once the marking became a border
    (the fleet card) and a glyph (the drawer) a coloured chip inside it was a second
    accent system saying the same thing.
    """
    for source in (RUN_CARD, RUN_NAV):
        text = _read(source)
        assert ">uber lmer<" in text, (
            f"{source.name} does not name the platform's own session, so the row "
            "that orchestrates the fleet reads as one of the runs in it"
        )
        assert "assistant" not in text.lower(), (
            f"{source.name} calls the supervisor an assistant; that spelling is the "
            "module's and the API's, and never an operator's (T31)"
        )
        assert "mdiRobot" in text, (
            f"{source.name}'s badge draws a different shape from the drawer that "
            "opens the same session, so nothing connects the two"
        )
        assert "color=" not in _badge(text), (
            f"{source.name}'s badge is painted again; the marking is the card's "
            "border and the drawer's glyph, and a coloured chip beside either is two "
            "accent systems on one row"
        )


def test_the_orchestrator_card_offers_the_chat():
    """The way into the drawer, from the row that is the drawer's other end.

    The bar's robot toggle exists on every view, but nothing on the fleet said
    the conversation existed (the operator: "i just want it to be more obvious
    that it exists"). The button only emits — the shell owns the drawer, exactly
    as it owns navigation for `open` — and it exists on the orchestrator row
    alone: a chat button on a worker's card would promise a conversation with
    the wrong party.
    """
    text = _read(RUN_CARD)
    button = re.search(r"<v-btn\b[^>]*v-if=\"orchestrator\"[^>]*>chat</v-btn>",
                       text, re.S)
    assert button, "the orchestrator card offers no way into the drawer"
    assert "$emit('open-chat')" in button.group(0), (
        "the chat button does something other than asking the shell to open "
        "the drawer"
    )
    assert 'aria-label="talk to uber lmer"' in button.group(0), (
        "the button's accessible name no longer matches the bar toggle's, so "
        "a screen reader meets two names for one conversation"
    )
    assert "mdiRobot" in button.group(0), (
        "the button dropped the mark that connects it to the drawer it opens"
    )
    app = _read(APP_VUE)
    assert app.count('@open-chat="uberOpen = true"') == 2, (
        "the shell does not open the drawer for both fleet sections' cards"
    )


def test_the_fleet_card_borders_the_row_that_is_not_a_run():
    """The operator's third pass, and the end of the tinted row: "i don't think solid
    background for the uber lmer row is the way -- lets instead do the following -
    give it an orange border in the main list view".

    A ground repaints everything the row says in order to say one thing about the row,
    and it arrived with a coloured badge on top of it. The border says the same thing
    at the edge and leaves the contents on the same surface as every other card.

    Three properties, and each of them is what keeps the marking from costing
    something. The colour is the theme's ``primary``, so the scheme switcher repaints
    it and no hex is stranded in a component. Nothing paints a *ground* any more, or
    the treatment the operator rejected is back under a new name. And the rule leaves
    the inline-start edge alone, because that edge is the attention stripe's
    (``style.css``): a ``border`` shorthand from a scoped selector outranks the
    stripe, so it would repaint a crashed orchestrator's red edge in the accent and
    take away the one marking this row shares with the rest of the list.
    """
    card = _read(RUN_CARD)
    assert card.count("orchestrator ? 'orchestrator-row' : null") == 1, (
        "the fleet card's border is not keyed on the row's kind exactly once, so what "
        "the flag decides cannot be read off the file"
    )
    assert "tone-edge" in card, (
        "the fleet card's two markings are no longer independent; the row's kind and "
        "the reason a human is needed are two questions with one answer each"
    )

    rule = _style_rule(card, ".orchestrator-row")
    # The three edges no stripe can claim, each named and each in the accent: a
    # v-card's own border-width is 0 and its border-color a neutral, so an edge this
    # rule leaves out is an edge that is simply not drawn.
    for declaration in (
        "border-block-width: 2px",
        "border-inline-end-width: 2px",
        "border-block-color: rgb(var(--v-theme-primary))",
        "border-inline-end-color: rgb(var(--v-theme-primary))",
    ):
        assert declaration in rule, (
            f"the fleet card's orchestrator border declares no {declaration!r}, so it "
            f"is drawn on fewer edges or in another colour than the accent "
            f"({rule.strip()!r})"
        )
    # And nothing thinner may creep onto one of them: 2px is what reads as deliberate
    # where a hairline reads as a rendering artifact, and a single thin edge among
    # thick ones reads as neither.
    widths = re.findall(r"border-[a-z-]*width:\s*([^;]+);", rule)
    assert set(widths) == {"2px"}, (
        f"the fleet card's orchestrator border is {widths} rather than 2px on every "
        f"edge it draws ({rule.strip()!r})"
    )
    assert "background" not in rule, (
        "the fleet card tints the orchestrator row again, which is the treatment the "
        "operator asked to be rid of"
    )
    # Never a tone in either rule: those five names are urgency, and this row is a
    # kind of row rather than a run in a state.
    for tone in ("success", "warning", "error", "tone-idle", "tone-done"):
        assert tone not in rule, (
            f"the fleet card borders the orchestrator row in {tone}, so the row that "
            "is not a run reads as a run in a state"
        )
    # The stripe's edge stays the stripe's, and the border still closes the box on a
    # card that has no stripe.
    assert "border-inline-start" not in rule, (
        f"the border claims the attention stripe's own edge ({rule.strip()!r}), so an "
        "orchestrator that crashed loses the red the rest of the list would show"
    )
    closed = _style_rule(card, ".orchestrator-row:not(.tone-edge)")
    assert "border-inline-start: 2px solid rgb(var(--v-theme-primary))" in closed, (
        "a card with no attention stripe is bordered on three sides only, which reads "
        f"as a card that failed to render ({closed.strip()!r})"
    )


def test_the_drawer_swaps_the_state_icon_for_the_uber_lmer_mark():
    """The other half of that pass: "in the side list view instead of the play icon
    show the robot icon".

    A 40px row has no room for a border and the operator did not ask for one there, so
    the drawer spends the channel it does have: the gutter. The filled robot is the
    uber lmer mark wherever this app means the supervising session — the outline
    variant is the driver chip's, i.e. a harness — so the two listings mark the same
    session with the same glyph.

    Pinned in both directions, because each failure is invisible on the screen the
    other one is fine on: without the swap the platform's own session wears a play
    button like any running run, and without the fallback *every* row becomes a robot
    and the drawer stops carrying urgency by shape at all.

    The row also keeps its ordinary ground: the tint that was here for one pass went
    with the fleet card's, and no scoped rule replaced it.
    """
    nav = _read(RUN_NAV)
    swap = ':icon="isOrchestrator(run) ? mdiRobot : icon(run)"'
    assert nav.count(swap) == 1, (
        f"the drawer's leading icon is not {swap} exactly once; the mark and the "
        "state icon are one binding, and either half missing is a gutter that lies"
    )
    assert "mdiRobot," in nav.split("} from '@mdi/js'")[0], (
        "the drawer's glyph is not the filled mdiRobot from @mdi/js, so the mark the "
        "fleet card badges this session with is not the one the drawer shows"
    )
    assert "orchestrator-row" not in nav, (
        "the drawer still classes the orchestrator row, which is the tint the "
        "operator ended; the mark there is the glyph in the gutter"
    )
    assert "background" not in nav[nav.index("<style"):], (
        "the drawer paints a ground again; every row in that list is drawn on the "
        "same surface, and the one that is not a run is marked in the gutter"
    )
    # And the state has not simply vanished from that row: it is still what the icon
    # is coloured by, and still the tooltip on the glyph.
    assert ':color="toneColor(tone(run))"' in nav and ':title="stateMeta(' in nav, (
        "the swapped glyph took the row's urgency with it; the colour and the tooltip "
        "are the two channels that keep it readable once the shape says kind instead"
    )


def test_the_mark_is_the_row_flag_and_nothing_a_listing_worked_out():
    """The daemon decides, and it decides from the registry kind (inventory.RunView).

    A listing that recognised the orchestrator by its taskdef or its target would
    badge any worker an operator spawned with those arguments — and the browser is
    the wrong place to hold that rule at all, because the fact it rests on (the
    session's ``kind``) deliberately does not cross into the payload.
    """
    for source in (RUN_CARD, RUN_NAV):
        text = _read(source)
        assert "run.orchestrator" in text, (
            f"{source.name} does not draw its badge from the row's own flag"
        )
        # On the code, not the prose: both files talk about the orchestrator and
        # the orchestrated at length, and a comment recognises nothing.
        code = _without_comments(text)
        for heuristic in ("orchestrate'", 'orchestrate"', "'fleet'", '"fleet"',
                          "run.kind", "run.taskdef ==="):
            assert heuristic not in code, (
                f"{source.name} works out what the orchestrator is for itself "
                f"({heuristic}); the daemon answers that from the registry kind"
            )


def test_a_row_that_is_not_the_orchestrator_renders_as_it_did_before():
    """Every other row is the case, so what the flag decides must be enumerable.

    It decides two things in each listing, and they are not the same two: the fleet
    card's badge and its border, the drawer's badge and the glyph in its gutter. The
    marking has changed three times and this is the property that has not — the flag
    is *read once* per listing, into a named derivation, and everything keyed on it is
    keyed on that. So a run that is not the platform's own session still renders
    exactly as it did — no tone, no reordering, nothing conditional it does not pay
    for — and the next thing to key on the orchestrator has to go through the same
    read.
    """
    for source, derivation, expected in (
        (RUN_CARD, "const orchestrator = computed(() => !!props.run.orchestrator)", 3),
        (RUN_NAV, "function isOrchestrator(run) {", 2),
    ):
        text = _read(source)
        assert text.count("run.orchestrator") == 1, (
            f"{source.name} reads the flag {text.count('run.orchestrator')} times; "
            "one read is what keeps the treatment one decision"
        )
        assert derivation in text, (
            f"{source.name} does not derive the row's kind in one named place, so "
            f"what the flag reaches cannot be read off the file ({derivation})"
        )
        # And what it reaches is enumerable per listing: the card's badge, border
        # and the chat button (the operator asked for a way in from the row —
        # "more obvious that it exists"); the drawer's badge and its leading icon.
        # Named here so the NEXT user of the flag (a tone, an order, a hidden
        # button) arrives as a failing test.
        keyed = re.findall(
            r"(?:v-if|:class|:icon)=\"[^\"]*"
            r"(?:isOrchestrator\(run\)|\borchestrator\b)[^\"]*\"",
            text,
        )
        assert len(keyed) == expected, (
            f"{source.name} keys {len(keyed)} things on the row's kind rather "
            f"than the {expected} this listing accounts for: {keyed}"
        )


# --- a target is a link only when it is one (T85) -----------------------------

def test_the_rows_target_is_a_link_only_when_there_is_somewhere_to_go():
    """The operator asked: "its target does link to a 404 currently ... probably
    nothing to link".

    ``:href`` on a target that is not an absolute URL is a *relative* link: a branch
    name, a prose target or the orchestrator's own ``fleet`` resolves against the
    page the app is served from and 404s. The chip still shows the target either
    way — the words are not the problem — and the gate lives in ``format.js``, so
    the row and the detail header cannot disagree about which targets are links.
    """
    row = _template()
    identity = row[row.index('<div class="d-flex flex-wrap align-center ga-3 mt-2">'):]
    identity = identity[:identity.index("<div", 1)]

    assert ':href="targetHref"' in identity, (
        "the row links the raw target, so a target that is not a URL is a relative "
        "link that 404s against the page"
    )
    assert 'v-if="run.target"' in identity, (
        "the chip is drawn only for a linkable target, so a run whose target is a "
        "branch or a sentence stops saying what it is working on"
    )
    assert ':append-icon="targetHref ? mdiOpenInNew : undefined"' in identity, (
        "an unlinked target still wears the icon that says the tap leaves the app"
    )

    assert "targetLink(props.run.target)" in _binding("targetHref"), (
        "the row decides what is linkable itself; that gate is format.js's, and a "
        "second copy of it is how the row and the header drift apart"
    )
    code = _without_comments(_script())
    for grammar in ("new URL(", "http", "startsWith"):
        assert grammar not in code, (
            f"RunCard.vue tests a target for itself ({grammar})"
        )


def test_which_targets_are_links_is_one_definition_and_it_reads_the_scheme():
    """The gate, run rather than read, in both directions.

    The interesting inputs are not the URLs — they are everything a taskdef target
    can also be, because those are what were being rendered as relative links. And
    the scheme is checked rather than merely the parse: ``new URL()`` is perfectly
    happy with ``mailto:`` and ``javascript:``, and this value becomes an ``href``.
    """
    _probe("""
    for (const target of [
      'https://gitlab.example.com/agents/global/-/merge_requests/7',
      'http://gitlab.example.com/agents/global',
    ]) {
      assert.equal(targetLink(target), target, `refused to link ${target}`)
    }
    for (const target of [
      null,
      '',
      'fleet',
      'issue-141',
      'feature/orchestrator-spec',
      'the fleet, as a whole',
      'gitlab.example.com/agents/global',
      'mailto:info@example.com',
      'javascript:alert(1)',
      'file:///etc/passwd',
    ]) {
      assert.equal(targetLink(target), null, `${target} was made into a link`)
    }
    """)


# --- what the run says about itself (T91) -------------------------------------

def test_the_row_says_what_the_run_records_about_itself():
    """The operator asked to see the run's own status in the listing.

    The chip is the *derived* state, and deriving it puts liveness first by design
    (spec D24): a live session is ``running`` whatever its state.yaml says. That is
    the right verdict and it is also how a row can read perfectly healthy while the
    record it was derived from says something else entirely — which nothing in the
    listing could show, because the committed status reached the payload and then
    rendered nowhere.

    It is labelled with the field it is: ``in-progress`` on its own, under a chip
    reading ``running``, is two state-shaped words and no way to tell which is the
    platform's and which is the run's. Same argument the identity line makes with its
    icons.

    The *phase* used to be joined onto this value and is not any more — the operator
    asked for it plainly (2026-07-29) and it went up to the identity line, where the
    test below pins it. The status is what is left here, and it is the half the
    argument was always about: it is the value that contradicts the chip.
    """
    row = _quiet_row()
    assert "{{ committed }}" in row, (
        "the dimmed line no longer carries what the run says about itself, so the "
        "listing shows only the state the platform derived"
    )

    binding = _binding("committed")
    assert "props.run.status" in binding, (
        "the run's own account of itself ignores props.run.status"
    )
    assert "status: " in binding, (
        "the status is rendered bare; beside a state chip an unlabelled word is "
        "indistinguishable from the state itself"
    )


def test_the_runs_own_words_stay_subordinate_to_the_state_chip():
    """Two state-shaped things in one row, and only one of them may win the glance.

    The chip is the signal — the row exists to answer "which run needs me" — so the
    committed record goes in the dimmed line *below* the identity, at that line's
    size and emphasis, and never in a chip of its own. A second chip beside the
    first would make the eye arbitrate between two words that disagree on purpose;
    dimmed and small, the disagreement is there to read once the chip has made you
    look.
    """
    template = _template()
    assert "committed" not in _first_row(), (
        "the run's own status shares the signal's flex line, where it both competes "
        "with the state chip and can wrap it away from the name"
    )
    assert template.index("toneColor(meta.tone)") < template.index("{{ committed }}"), (
        "the committed record is read before the state chip"
    )
    assert template.index("{{ run.taskdef }}") < template.index("{{ committed }}"), (
        "the committed record sits above what the run IS; it is the detail you read "
        "after deciding to look at a row, not the thing that identifies it"
    )

    row = _quiet_row()
    assert "<v-chip" not in row, (
        "the run's own status is a chip now, so the row has two state-shaped chips "
        "and the derived one no longer obviously wins"
    )
    opening = row[:row.index(">") + 1]
    for quiet in ("text-body-small", "text-medium-emphasis"):
        assert quiet in opening, (
            f"the line carrying the committed record lost {quiet}, so the run's own "
            "words read as loudly as the signal above them"
        )


def test_a_run_that_has_recorded_nothing_renders_nothing():
    """The common case: null, and no slot held open for it.

    A session in its first seconds has no state.yaml at all, and a run dir can exist
    before anything is written into it — so a placeholder, or a separator with
    nothing on one side of it, would be on the row far more often than the values
    are. The join is in the binding and the template interpolates it whole, which is
    what makes a stray "· " unrepresentable rather than merely absent today.
    """
    text = _read(RUN_CARD)
    assert text.count('v-if="committed"') == 1, (
        "the line is drawn by something other than whether there is anything to "
        "say, so a run that recorded nothing still pays for it"
    )

    binding = _binding("committed")
    assert ": null" in binding, (
        "a run that has recorded no status gets '' or 'status: undefined' rather "
        "than null; the value a listing tests has to be the value it renders"
    )

    element = re.search(
        r'<span\b[^>]*v-if="committed"[^>]*>\{\{ committed \}\}</span>',
        _quiet_row(), re.S,
    )
    assert element, (
        "the committed record is not one span rendering the joined value; a "
        "separator or a label spelled in the template can outlive the part it "
        "belongs to, which is how an empty '·' ends up on a bootstrapping run"
    )


# --- where the work has got to (2026-07-29) -----------------------------------

def test_both_listings_say_where_the_work_has_got_to():
    """The operator asked: "id also like the listings to show the run phase".

    The phase already crossed in the fleet payload (inventory.RunView), and both
    listings had reasons not to show it: the card joined it onto the dimmed committed
    line where it was one of five grey facts, and the drawer left it out on purpose
    (a third segment under a 40px row's name). The ask overrules both, so it is
    rendered in each listing's own idiom for a fact about a run — the card's identity
    line, with an icon and a tooltip like the taskdef and the repo beside it, and the
    drawer's dimmed second line, after the state word and before the age.

    What it must not become is a second signal. A phase is taskdef-defined free text
    ("execution", "cleanup"), and the tone ramp is urgency: painted or chipped, it
    would compete with the one thing these rows exist to say.
    """
    card, nav = _read(RUN_CARD), _read(RUN_NAV)

    assert "{{ run.phase }}" in card, (
        "the fleet card renders the phase nowhere, so the listing does not say where "
        "a run has got to"
    )
    assert "{{ run.phase }}" not in _quiet_row(), (
        "the phase is back in the dimmed line among the run's other grey facts, "
        "which is where the operator could not read it"
    )
    identity = card[card.index('<div class="d-flex flex-wrap align-center ga-3 mt-2">'):]
    identity = identity[:identity.index("<div", 1)]
    assert "{{ run.phase }}" in identity, (
        "the phase is not on the line that says what a run IS, beside the taskdef "
        "and the repo"
    )
    assert 'title="phase"' in identity, (
        "the value is rendered with nothing saying which field it is; 'cleanup' on "
        "its own is a word an operator has to guess at"
    )
    assert "<v-chip" not in re.search(
        r"<span[^>]*v-if=\"run\.phase\".*?</span>", identity, re.S,
    ).group(0), "the phase is a chip, which is the shape this row's signal has"

    assert "{{ run.phase }}" in nav, (
        "the drawer still leaves the phase out; the operator asked for it in both "
        "listings, and this is the one on screen while a run is open"
    )
    subtitle = nav[nav.index("<v-list-item-subtitle>"):]
    subtitle = subtitle[:subtitle.index("</v-list-item-subtitle>")]
    assert subtitle.index("line(run)") < subtitle.index("run.phase"), (
        "the phase is read before the state word, which is the one thing in this "
        "row that must never be cut"
    )
    assert subtitle.index("run.phase") < subtitle.index("ago(run.updated"), (
        "the phase sits after the age, so the row's two time facts are separated by "
        "something that is not one"
    )
    element = re.search(r"<span v-if=\"run\.phase\"[^>]*>", subtitle)
    assert element and "text-medium-emphasis" in element.group(0), (
        "the phase is rendered at full emphasis in the drawer, where it reads as "
        "loudly as the state word beside it — and this row opts subtitles out of "
        "Vuetify's own dimming, so nothing else dims it"
    )
    assert 'title="phase"' in element.group(0), (
        "the drawer renders the value with nothing saying which field it is"
    )


def test_a_run_with_no_phase_renders_no_phase_in_either_listing():
    """Most runs, and the failure is a stray separator or an empty slot.

    ``phase`` is null for a chat run and for every run in its first seconds
    (inventory.RunView), so both listings have to be absent rather than empty — and
    in the drawer the separator lives *inside* the conditional element, because a
    "· " that outlives the value it introduces is exactly what a second span would
    leave behind.
    """
    for source in (RUN_CARD, RUN_NAV):
        text = _read(source)
        assert text.count('v-if="run.phase"') == 1, (
            f"{source.name} draws the phase without asking whether there is one, or "
            "asks twice — a placeholder for a fact nobody has"
        )
    nav = _read(RUN_NAV)
    element = re.search(
        r"<span v-if=\"run\.phase\"[^>]*>(.*?)</span>", nav, re.S,
    )
    assert element and "·" in element.group(1), (
        "the drawer's separator is outside the element the phase is drawn in, so a "
        "run with no phase renders a bare '·' between the state and the age"
    )


# --- how long the session has been quiet (T95) --------------------------------
#
# The gap in the row: `state` is derived with liveness first (spec D24), so the chip
# reads `running` from the moment a session starts until the moment it exits. A run
# that finished its work and is sitting at its prompt therefore looks exactly like
# one that is working, and the only way to tell used to be to open the terminal and
# read the scrollback — which is precisely what the fleet view exists to save.
#
# "idle 22m" closes it, in the T91 idiom: the dimmed line, no chip, no colour, and
# nothing at all when it is not known (an older image, an unreachable container, a
# run with no session — all ordinary).

def test_the_row_says_how_long_the_session_has_been_quiet():
    """The fact the state chip cannot carry, rendered where the quiet facts go."""
    row = _quiet_row()
    assert "{{ idle }}" in row, (
        "the dimmed line no longer says how long the session has been quiet, so a "
        "finished-but-still-running session is indistinguishable from a busy one"
    )

    binding = _binding("idle")
    assert "activity?.idle_seconds" in binding, (
        "the idle label no longer reads the daemon's measurement out of the "
        "session block (inventory.SESSION_FIELDS)"
    )
    assert "duration(" in binding, (
        "the span is formatted by something other than format.js's duration(), so "
        "the row and the age beside it can disagree about when minutes become hours"
    )
    assert "idle " in binding, (
        "the value is rendered bare; in a line that already carries two other "
        "durations, a lone '22m' says nothing about what was idle"
    )


def test_the_idle_label_is_the_daemons_seconds_and_never_an_age_computed_here():
    """The one way this goes quietly wrong: recomputing it against this clock.

    The reading was measured by one monotonic clock in the process that saw the
    output (lmer_cli.supervisor). Deriving it here from ``last_output_at`` and
    ``Date.now()`` would put a phone's clock in the loop, and a device a few minutes
    fast would report every busy session as idle. The timestamp is the tooltip, and
    that is all it is.
    """
    binding = _binding("idle")
    for computed_here in ("ago(", "Date.", "props.now", "now)"):
        assert computed_here not in binding, (
            f"the idle label is computed with {computed_here} — against this "
            "device's clock rather than from the daemon's measurement"
        )
    assert "last_output_at" not in binding, (
        "the label reads the timestamp; it is the tooltip, because a rendered age "
        "would depend on the reader's clock"
    )
    assert "last_output_at" in _quiet_row(), (
        "the moment the container dated the output to is not shown anywhere, so "
        "there is no way to see what 'idle 22m' was measured against"
    )


def test_the_idle_reading_stays_subordinate_to_the_state_chip():
    """Same subordination T91 pins for the run's own words, and for the same reason.

    The chip is the platform's verdict and has to keep winning the glance; this is a
    fact the verdict does not contain, not a competing verdict. A chip of its own —
    or a place on the signal's flex line — would put a third state-shaped thing in
    the row for the eye to arbitrate between, and could wrap the state away from the
    name it belongs to.
    """
    template = _template()
    assert "idle" not in _without_comments(_first_row()), (
        "the idle reading shares the signal's flex line, where it competes with the "
        "state chip and can wrap it away from the name"
    )
    assert template.index("toneColor(meta.tone)") < template.index("{{ idle }}"), (
        "the idle reading is read before the state chip"
    )
    row = _quiet_row()
    assert "<v-chip" not in row, "the idle reading became a chip"
    for quiet in ("text-body-small", "text-medium-emphasis"):
        assert quiet in row[:row.index(">") + 1]


def test_a_session_that_reports_no_idleness_renders_nothing():
    """Absent is ordinary, so absent must cost the row nothing.

    Three ways to have no reading and all of them common: a run with no live
    session, a container that did not answer the daemon's poll, and a session whose
    image predates the fact. None of them may render a slot, a placeholder, or an
    "idle 0s" — which would say the harness had just done something.
    """
    text = _read(RUN_CARD)
    assert text.count('v-if="idle"') == 1, (
        "the span is drawn by something other than whether there is a reading"
    )

    binding = _binding("idle")
    assert ": null" in binding or "|| null" in binding, (
        "an unknown reading returns something other than null, so the value a "
        "listing tests is no longer the value it renders"
    )
    element = re.search(
        r'<span\b[^>]*v-if="idle"[^>]*>\{\{ idle \}\}</span>', _quiet_row(), re.S,
    )
    assert element, (
        "the idle reading is not one span rendering the whole label; a word spelled "
        "in the template can outlive the value beside it"
    )


def test_a_duration_is_the_shortest_thing_that_answers_how_long():
    """Executed, because this one is arithmetic and reading it proves nothing.

    The thresholds are `ago()`'s on purpose — the two sit in the same dimmed line,
    and a row saying "idle 50m · 1h ago" for two moments a minute apart is a row
    that reads as broken.
    """
    _probe("""
    assert.equal(duration(0), '0s', 'a harness that just drew something')
    assert.equal(duration(12.4), '12s')
    assert.equal(duration(44), '44s')
    assert.equal(duration(45), '1m', 'the same hinge ago() turns at')
    assert.equal(duration(1320), '22m', 'the story this exists for: idle 22m')
    assert.equal(duration(2640), '44m')
    assert.equal(duration(2700), '1h', 'the same hinge ago() turns at')
    assert.equal(duration(126000), '35h')
    assert.equal(duration(129600), '2d', 'the last hinge, also ago()\\'s')
    // Absent stays absent, in every shape the payload can carry it.
    for (const nothing of [null, undefined, NaN, 'later']) {
      assert.equal(duration(nothing), null, `${nothing} rendered a duration`)
    }
    // A clock that stepped must not render a negative span.
    assert.equal(duration(-30), '0s')
    """)


# --- the note is bounded and the row is not (operator feedback) ----------------
#
# The note is the one unbounded thing in this row: a session waiting on a reply puts
# its whole question in it, and questions are written to be read, not glanced at. On
# a live fleet that turned one row into most of a phone screen — "waiting on your
# reply displayed inside the runlist entry shows a long blob of text - given that i
# cannot act on it from there anyway, lets truncate that to something sensible".
#
# A clamp rather than a cut string, and both halves of that matter: the DOM still
# holds the whole note (the fleet's own search reads what the row shows), and no
# character count has to be invented for a proportional font on an unknown screen.


def test_a_wall_of_prose_in_the_note_cannot_take_the_whole_list():
    """Bounded to a couple of lines, with the label outside the bound.

    Which is the half that is easy to lose: clamp the alert instead of the note and
    a long attention label — "is waiting on your reply" on a 390px screen — eats the
    lines the note was supposed to get, so the row says less the more urgent it is.
    """
    template = _template()
    element = re.search(
        r'<div\b[^>]*class="attention-note"[^>]*>\{\{ run\.attention\.note \}\}</div>',
        template, re.S,
    )
    assert element, (
        "the note is not one clamped element rendering the whole value; a row that "
        "cuts the string instead has to invent a character count, and the search "
        "over this list can then no longer see what the row shows"
    )
    assert ":title=\"run.attention.note\"" in element.group(0), (
        "the clamped note is not readable in full anywhere on this row"
    )
    assert "attention-note" not in template[:template.index("attentionLabel")], (
        "the clamp is around the label too, so the more urgent the row the less of "
        "the note it shows"
    )

    rule = _style_rule(_read(RUN_CARD), ".attention-note")
    assert "-webkit-line-clamp: 2" in rule, (
        "the note is not clamped to a fixed number of lines, so one agent's "
        "question sizes the whole list"
    )
    assert "display: -webkit-box" in rule and "-webkit-box-orient: vertical" in rule, (
        "a line clamp without the box display and orientation is ignored by every "
        "browser an operator is holding"
    )
    assert "overflow: hidden" in rule, "the clamped lines are still drawn"


# --- getting a finished run out of the list (T101) -----------------------------


def test_forget_is_offered_only_on_a_run_that_has_ended():
    """The gate, and it is a *liveness* gate before it is a state one (spec D24).

    Two ways to get this wrong and only one of them is visible in a diff. The state
    is derived with liveness outranking the committed record, so a live session
    reads `running` however finished its own state.yaml says it is — which is what
    keeps a working container out of this — and `live` is tested beside it so the row
    fails closed if that ever stops being true. A row with a session has a different
    verb: winding it down.

    The states themselves are the second half: a pause is not an ending. `dormant` is
    a run between sessions, `waiting_on_you` and `yielded` are runs waiting on the
    operator, and `crashed` is the one whose leftover entry *is* the record of the
    crash — a one-tap way to hide that news is not a feature.
    """
    from lmer_platform.inventory import RUN_STATES

    binding = _binding("forgettable")
    assert "!props.run.live" in binding, (
        "the row can offer to forget a run with a live session, which drops the "
        "platform's own handle on a running container"
    )
    assert "ENDED_STATES.includes(props.run.state)" in binding, (
        "the offer is not gated on the run having ended"
    )

    block = re.search(r"const ENDED_STATES = \[(.*?)\]", _script(), re.S)
    assert block, "RunCard.vue no longer says which states have ended"
    ended = re.findall(r"'(\w+)'", block.group(1))
    assert set(ended) == {"complete", "failed"}, (
        f"the ended states are {ended}; each of these is a claim that nothing more "
        "will happen to the run, and has to be argued for rather than added"
    )
    for state in ended:
        assert state in RUN_STATES, (
            f"{state!r} is not a state the daemon can emit (inventory.RUN_STATES), "
            "so the offer is made on a word nothing produces"
        )
    for waiting in ("running", "detached", "dormant", "waiting_on_you", "crashed"):
        assert waiting not in ended, (
            f"{waiting!r} is treated as an ending; it is a run that is waiting, "
            "working, or the record of a crash"
        )


def test_the_row_asks_for_the_forget_and_never_does_it():
    """The row owns no list, so it cannot own the removal from one.

    The undo window lives in the shell (tests/test_platform_web_app.py), which is
    also where the row could be put back. A card that called the route itself would
    have to hide itself from a list it does not own, and would forget a run per row
    with no window at all.
    """
    text = _read(RUN_CARD)
    assert "defineEmits(['open', 'forget', 'open-chat'])" in text, (
        "the row does not declare the verbs it emits"
    )
    assert "$emit('forget', run)" in _template(), "nothing asks for the forget"
    for reaching in ("api.js", "forgetRun", "fetch("):
        assert reaching not in text, (
            f"the row talks to the daemon itself ({reaching}); the shell owns the "
            "undo window, and a row that sends its own request has none"
        )


def test_the_way_out_of_a_run_is_last_and_colourless():
    """It is not a way *into* anything, and it is not the row's signal.

    Ordering first: the button that opens the run stays the one at the end of the
    reading order an operator scans for, so the way out goes after it. Colour
    second — the ramp is urgency and the accent is "go and see this", and a red
    button on a finished row would make a calm row the loudest thing in the list.
    The palette is pinned exhaustively by SANCTIONED_COLOURS; this pins the one
    property that table cannot see, which is that this button is drawn at all.
    """
    template = _template()
    assert template.index(">enter<") < template.index(">forget<"), (
        "the way out of the run comes before the way into it"
    )
    button = re.search(r"<v-btn\b[^>]*v-if=\"forgettable\".*?</v-btn>", template, re.S)
    assert button, "the forget button is not the one drawn by the gate"
    assert template.count('v-if="forgettable"') == 1, (
        "something else in the row is drawn by the same gate, so a run that has "
        "not ended pays for it"
    )
    assert "color" not in button.group(0), (
        "the forget button is painted; on a finished row that puts the loudest "
        "thing in the list on the run that needs the least attention"
    )
    assert "title=" in button.group(0), (
        "nothing says what forgetting does to the run — the operator's fear here "
        "is that it deletes work"
    )


# --- the row says when anyone last looked at the run (issue #244) --------------
#
# The fleet view's half of the check-in work: the daemon tells the assistant which
# runs have gone unchecked, and the operator asked to be able to see the same
# thing without asking the assistant. What these keep is that the row *reports* a
# verdict rather than reaching one, and that showing it costs the row's signal
# nothing — an unpainted word in the dimmed line, like every other addition here.

def test_the_row_reports_the_daemons_staleness_rather_than_deciding_it():
    """A row that decided for itself would mark runs no digest ever names.

    The window, the eligibility rules and the clock all live in
    ``lmer_platform.checkin``; the payload carries the answer. A component that
    compared an age against a number of its own would drift from the digest the
    assistant acts on the first time either changed.
    """
    text = _read(RUN_CARD)
    assert "checkinLabel(props.run.checkin" in text, (
        "the row is not reading the daemon's own check-in block"
    )
    assert not re.search(r"3600|checkin_window", text), (
        "the row has a window of its own, so the marker and the digest can "
        "disagree about which runs have gone quiet"
    )


def test_the_check_in_marker_is_not_painted():
    """Colour in this app is urgency, and a quiet run is not a crashed one."""
    text = _read(RUN_CARD)
    marker = re.search(
        r'<span\s+v-if="checkin".*?</span>', text, re.S,
    )
    assert marker, "the check-in span is gone from the row"
    assert "color=" not in marker.group(0), (
        "the check-in marker took a colour, which puts a second urgency signal "
        "in a row whose ramp already means something"
    )
    assert "text-high-emphasis" in marker.group(0), (
        "a stale run has to be findable while scanning the list — the emphasis "
        "is what does that without reaching for the ramp"
    )


def test_the_two_check_in_wordings_are_two_different_facts():
    """"checked" must mean something read the run; it is not a synonym for an age.

    The clock a stale run reports runs from whatever last restarted it — a read,
    a digest already spooled, or first sight of the run — so wording that as
    "checked" would have the row inventing a look nobody took.
    """
    _probe("""
    const now = Date.parse('2026-08-06T12:00:00Z')
    assert.equal(
      checkinLabel({checked_at: '2026-08-06T11:48:00Z', age_seconds: 720,
                    stale: false}, now),
      'checked 12m ago')
    assert.equal(
      checkinLabel({checked_at: null, age_seconds: 15600, stale: true}, now),
      'unchecked 4h')
    assert.equal(
      checkinLabel({checked_at: '2026-08-06T07:40:00Z', age_seconds: 15600,
                    stale: true}, now),
      'unchecked 4h',
      'a run that was read hours ago and has gone stale is unchecked, not checked')
    assert.equal(
      checkinLabel({checked_at: null, age_seconds: 30, stale: false}, now), null,
      'a run the platform has only just seen says nothing at all')
    assert.equal(checkinLabel(null, now), null,
      'a payload from a daemon that predates check-ins renders nothing')
    """)
