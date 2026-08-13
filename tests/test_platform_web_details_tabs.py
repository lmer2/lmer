"""Guards on the run detail view's tabs and the choice it remembers (T49).

Source-level, like :mod:`tests.test_platform_web_app` and
:mod:`tests.test_platform_web_terminal`, plus one *executed* probe — because half
of this slice is a preference read back out of a browser, and that half has a
failure mode reading the source cannot rule out.

The view used to stack everything: the run's facts, the answer box, the two
verbs that end a session, the resume form, the operator channel, a transcript
that runs to hundreds of turns, the terminal, the events, the forget button. It is
three tabs now — overview, lmer, exit — with the session's three views as panes of
the middle one. Each test below pins one decision whose loss has no symptom in a
build:

- the tab bar and the panels read the same model. They render fine when they do
  not; the selected tab is simply not the visible one
- the lmer panes are the *session's* three views, and the third one is this run's
  ask channel — not the supervisor chat, which is a drawer for the whole fleet and
  a different slice (T31)
- neither long view is rendered a second time outside its pane, which is what
  stacked looked like and would undo both the bounded box and the chunk
- the terminal pane stays lazy, so opening a run still does not fetch xterm
- the run's identity, the reason it needs a human, and the box that answers a
  question stay *above* the tabs. This is new and it is the price of remembering
  the tab: this view no longer decides what the operator lands on, so anything
  that answers "why am I looking at this run" cannot be behind a tab
- the identity in that header is the taskdef, the repo and the target (T61), read
  through ``format.js``'s helpers rather than parsed again here, and it stays one
  wrapping line — those three fields are most of what made the overview worth
  opening, and a card of them in the header would push the attention alert and the
  answer box off a phone screen, which is the one thing being above the tabs is
  for
- the ports the run published ride that same line rather than the overview's
  session card, because a run that published one is a run with something to look
  at and the tab it was behind is not the tab it is looked at from
- the heading line is the *title* this orchestrator gave the run when it gave it
  one, with the label under it and the run's own name never lost (T65) — the
  operator asked: "if meta.title is set it should be used in the details header and
  the listings", because a title visible only inside its own tab identifies nothing
- the run that IS the orchestrator says so on that heading line (T85), from the flag
  the daemon puts on the row — and nothing else in the view is keyed on it, because
  what an operator may do to that session is a different question from which run
  they are looking at
- a target is a link only when it is an absolute URL, in the header and in the
  overview alike: anchored unconditionally, a branch name or a prose target was a
  *relative* link that 404'd against the page, and the gate is ``format.js``'s so
  that this view and the fleet row cannot disagree about what is clickable
- the overview's facts obey absent-renders-nothing. A run that has recorded no
  status yet — every run in its first seconds — used to get a row with an em dash
  in it, which is a placeholder standing in for a fact nobody has
- the session card carries the idle reading (T95) in the fleet row's subordinate
  idiom, because the state above it says ``running`` from the moment a session
  starts until it exits and cannot tell a working session from a finished one
- a remembered tab is validated against the tabs that exist. Vuetify renders no
  panel for a value that selects none, so a renamed or dropped tab — or another
  version of this app writing the same key — leaves a *blank run view*, which is
  much worse than a forgotten preference
- reading and writing that preference survives a `localStorage` that throws
  rather than answering, which private browsing and some embedded webviews do
- the switch the platform turns off by itself is not stored as the operator's
  choice
- every tab of both rows draws a shape beside its word (T96) — the operator asked:
  "tabs in detail view should get some icons" — and where the concept already has
  one in this app, it is that one. Pinned name by name: swapped shapes render a
  tidy bar describing the wrong panels
- the attention alert says a reply is wanted and leaves the question itself to the
  surface that can answer it, which renders the same text a few lines below. Only
  for the two reasons whose note *is* the question, and only when that surface is
  there — a crash's note is the only account of a crash on this page
- the run row shows the directory when one was found and the run's *key* when none
  was, and never assembles ``runs/<slug>`` itself (T90/T96): a named run's
  directory is ``runs/<slug>--<name>``, so the composed path was wrong for most
  runs and unopenable for the rest

How any of it looks, and whether two rows of tabs crowd a phone, are verified by
building the bundle and by live test LT3 on a real phone.
"""

import json
import re
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain
from tests.test_platform_web_app import ALLOWED_STORAGE_KEYS

WEB = Path(__file__).resolve().parent.parent / "web"
RUN_DETAIL = WEB / "src" / "components" / "RunDetail.vue"
TERMINAL = WEB / "src" / "components" / "Terminal.vue"
PREFERENCES = WEB / "src" / "preferences.js"
STYLE = WEB / "src" / "style.css"

#: The fence around the part of RunDetail.vue that is plain JavaScript — the keys,
#: the tab lists, and the four functions that read and write them. Lifted verbatim
#: and executed below, because a copy in this file would pass forever while the
#: component's own copy drifted into trusting what it read. Prefixes, because both
#: lines are padded out to the column limit with dashes.
CHOICE_START = (
    "// --- remembered choice (extracted by tests/test_platform_web_details_tabs.py)"
)
CHOICE_END = "// --- end of remembered choice"

#: What each tab of the two rows draws beside its label, pinned name by name (T96).
#: Counted, a swap between two tabs renders a perfectly tidy bar that says the
#: wrong thing — the same argument ``RunNav.vue``'s state icons are pinned by.
#: Three of the seven reuse a shape the app already speaks for the same concept:
#: the harness outline the fleet card puts on the model driving a session, the
#: console for the emulator, and the ask channel's question mark for the channel.
TAB_ICONS = {
    "overview": "mdiInformationOutline",
    "meta": "mdiTextBoxOutline",
    "lmer": "mdiRobotOutline",
    "exit": "mdiExitRun",
}
PANE_ICONS = {
    "conversation": "mdiForumOutline",
    "terminal": "mdiConsole",
    "chat": "mdiCommentQuestionOutline",
}
#: The word each tab keeps. Icons were additive: seven glyphs and no words is a
#: puzzle, and "conversation" and "operator chat" are two things a shape alone does
#: not separate.
TAB_LABELS = {
    "overview": "overview",
    "meta": "meta",
    "lmer": "lmer",
    "exit": "exit",
    "conversation": "conversation",
    "terminal": "terminal",
    "chat": "operator chat",
}


def _read(path):
    return path.read_text(encoding="utf-8")


def _function_body(text, signature):
    """Source of one top-level function in a ``<script setup>`` block.

    Same helper as :mod:`tests.test_platform_web_terminal`: every function in these
    components is top-level, so a ``}`` in column zero ends one.
    """
    start = text.index(signature)
    return text[start:text.index("\n}\n", start)]


def _element(text, tag, start):
    """The markup of the *tag* element that opens at *start*, closing tag included.

    Depth-counted, and the lookahead is what makes it usable here at all:
    ``<v-tabs-window-item`` begins with ``<v-tabs-window``, so a plain substring
    scan counts every panel as another window and never finds the close.
    """
    pattern = re.compile(rf"<{tag}(?=[\s/>])|</{tag}>")
    depth = 0
    for match in pattern.finditer(text, start):
        if match.group(0).startswith("</"):
            depth -= 1
            if not depth:
                return text[start:match.end()]
        else:
            depth += 1
    raise AssertionError(f"<{tag}> opened at {start} is never closed")


def _window(model):
    """The ``<v-tabs-window>`` bound to *model*, whole."""
    text = _read(RUN_DETAIL)
    opening = re.search(rf'<v-tabs-window\s+v-model="{model}"', text)
    assert opening, f"RunDetail.vue has no <v-tabs-window> reading {model!r}"
    return _element(text, "v-tabs-window", opening.start())


def _panels(model):
    """The panels of the window bound to *model*, keyed by what selects each.

    Depth-aware on purpose: the lmer panel *contains* a second window, and a flat
    split would hand its panes back as if they were top-level tabs — which is the
    shape of this file's central mistake, so the helper must not make it.
    """
    window = _window(model)
    tokens = re.compile(
        r"<v-tabs-window(?=[\s/>])|</v-tabs-window>|<v-tabs-window-item(?=[\s/>])"
    )
    panels = {}
    depth = 0
    for token in tokens.finditer(window):
        found = token.group(0)
        if found == "</v-tabs-window>":
            depth -= 1
        elif found.startswith("<v-tabs-window-item"):
            if depth != 1:
                continue
            panel = _element(window, "v-tabs-window-item", token.start())
            value = re.search(r'value="([\w-]+)"', panel)
            assert value, "a panel with no value cannot be selected by a tab"
            panels[value.group(1)] = panel
        else:
            depth += 1
    return panels


def _tab_pair(model):
    """The tab bar and the window for *model*, asserted to be one pair.

    Rendering is unaffected when these disagree: both halves work, and the tab that
    is underlined is simply not the panel that is showing.
    """
    text = _read(RUN_DETAIL)
    bar = re.search(rf'<v-tabs\s+v-model="{model}"', text)
    assert bar, f"RunDetail.vue has no <v-tabs> reading {model!r}"
    return bar, _window(model)


def _header():
    """Everything the view renders above the tab bar.

    Which is the same thing as everything a remembered tab cannot hide, so it is
    the region every assertion about the header is really about.
    """
    text = _read(RUN_DETAIL)
    return text[text.index("<template>"):text.index('<v-tabs v-model="tab"')]


def _without_comments(text):
    """*text* with its markup and line comments removed.

    For the assertions that are about what the code *does*: this view explains
    itself at length, and a token in a comment reaches nothing. Same treatment
    :mod:`tests.test_platform_lifecycle` gives its hex scan of the same file.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def _script():
    return _read(RUN_DETAIL)[:_read(RUN_DETAIL).index("</script>")]


def _binding(name):
    """Source of one top-level binding in the ``<script setup>`` block.

    From its ``const`` to whatever starts at column zero after it — the helper
    :mod:`tests.test_platform_web_runcard` uses, and for its reason: these are
    arrow bodies, so they end in ``)`` rather than in a brace of their own, and
    what is under test is inside one.
    """
    script = _script()
    rest = script[script.index(f"const {name} = "):]
    match = re.search(r"\n(?=(?:const|function|//|/\*)\s)", rest)
    return rest[:match.start()] if match else rest


def _mdi_imports():
    """The icon names ``RunDetail.vue`` takes from ``@mdi/js``."""
    match = re.search(r"import \{([^}]*)\} from '@mdi/js'", _read(RUN_DETAIL), re.S)
    assert match, "RunDetail.vue no longer imports any icons"
    return {name.strip() for name in match.group(1).split(",") if name.strip()}


def _tab(value):
    """The ``<v-tab>`` that selects *value*, whole."""
    detail = _read(RUN_DETAIL)
    start = detail.find(f'<v-tab value="{value}"')
    assert start != -1, f"RunDetail.vue has no tab selecting {value!r}"
    return _element(detail, "v-tab", start)


def _format_imports():
    """The names ``RunDetail.vue`` takes from ``format.js``."""
    match = re.search(
        r"import \{([^}]*)\} from '\.\./format\.js'", _read(RUN_DETAIL), re.S,
    )
    assert match, "RunDetail.vue no longer imports the shared presentation helpers"
    return {name.strip() for name in match.group(1).split(",") if name.strip()}


def _identity_line():
    """The header's identity row, from its own ``<div`` to that div's close.

    Found by the taskdef rather than by its classes: the classes are layout and
    will be edited, while "the line the taskdef is on" is the thing under test.
    """
    header = _header()
    taskdef = header.index("{{ run.taskdef }}")
    return _element(header, "div", header.rindex('<div class="d-flex', 0, taskdef))


def _string_constant(text, name):
    match = re.search(rf"const {name} = '([^']*)'", text)
    assert match, f"no {name} constant"
    return match.group(1)


# --- three tabs, and one model each ------------------------------------------

def test_the_run_detail_view_is_four_top_level_tabs():
    """The operator's ask, and the shape everything below depends on.

    Four since T52: what a run is *about* — a title and a description this
    orchestrator keeps for it — is a tab of its own, because a description is
    paragraphs and at the top of the overview it would push the facts that answer
    "what is it doing" off a phone screen.

    The names are pinned rather than counted, because every test below reaches for
    one of them by name: a rename this file did not notice would leave those
    describing a panel they are no longer looking at.
    """
    bar, _ = _tab_pair("tab")
    detail = _read(RUN_DETAIL)
    panels = _panels("tab")

    assert set(panels) == {"overview", "meta", "lmer", "exit"}, (
        f"the top-level tabs are {sorted(panels)}"
    )
    for value in panels:
        assert f'<v-tab value="{value}"' in detail, f"no tab selects {value!r}"
    # The bar comes first, so the panels are what the operator sees underneath the
    # thing they tapped rather than above it.
    assert bar.start() < detail.index("<v-tabs-window")


def test_the_lmer_tab_holds_the_three_session_views():
    """One session, three ways of reading it, and nobody needs two at once.

    The third one is the channel's *record* since T40 (``AskHistory``): what is
    waiting on an answer is docked above the tabs, where a remembered tab cannot
    hide it, and what was said is here. The pane earns its place next to the other
    two rather than above them — it is usually empty, so stacked it was a heading
    that was mostly absent and occasionally the most important thing on the page.
    """
    _tab_pair("pane")
    detail = _read(RUN_DETAIL)
    panes = _panels("pane")

    assert set(panes) == {"conversation", "terminal", "chat"}, (
        f"the lmer panes are {sorted(panes)}"
    )
    assert "<Chat" in panes["conversation"]
    assert "<Terminal" in panes["terminal"]
    assert "<AskHistory" in panes["chat"]
    for value in panes:
        assert f'<v-tab value="{value}"' in detail, f"no tab selects {value!r}"

    # And the whole group lives inside the lmer panel, not beside it.
    lmer = _panels("tab")["lmer"]
    assert '<v-tabs v-model="pane"' in lmer, "the session views are not in the tab"
    assert "operator chat" in lmer, "the third pane is unlabelled"


# --- a shape on every tab (T96) ----------------------------------------------

def test_every_tab_in_both_rows_draws_a_shape_beside_its_word():
    """The operator asked: "tabs in detail view should get some icons."

    Both rows, and every tab of both: an iconless tab in a row of icons reads as
    the disabled one, which is the shape of "nothing here" this app avoids
    everywhere else it draws a gutter.

    The map is pinned name by name rather than counted, for the reason
    ``RunNav.vue``'s state icons are: two tabs whose shapes were swapped render a
    tidy bar that describes the wrong panels, and nothing in a build says so.
    """
    imported = _mdi_imports()
    icons = {**TAB_ICONS, **PANE_ICONS}

    for value, icon in icons.items():
        tab = _tab(value)
        assert f':icon="{icon}"' in tab, (
            f"the {value!r} tab does not draw {icon}: {' '.join(tab.split())}"
        )
        assert icon in imported, (
            f"{icon} is drawn but not imported from @mdi/js, so the tab renders "
            "a hole where every other one has a shape"
        )
        # Tight against the label and before it, which is what makes the icon part
        # of the tab rather than a second thing on the line.
        assert f"/>{TAB_LABELS[value]}</v-tab>" in tab, (
            f"the {value!r} tab lost its word, or the icon no longer sits in front "
            f"of it: {' '.join(tab.split())}"
        )

    assert len(set(icons.values())) == len(icons), (
        f"two tabs of this view draw the same shape: {icons}"
    )


def test_the_lmer_panes_speak_the_icon_language_the_rest_of_the_app_does():
    """Where the concept already has a shape, the tab reuses it.

    A run's session is drawn as the harness driving it on the fleet card, and the
    ask channel draws a question mark on every entry of itself — so the pane that
    renders that channel's record and the tab that holds the session's views are
    not a second drawing of either. Asserted against the components that own those
    shapes rather than restated here, so a change of vocabulary over there arrives
    as a failure here instead of a quiet divergence.
    """
    components = RUN_DETAIL.parent

    card = _read(components / "RunCard.vue")
    assert TAB_ICONS["lmer"] in card, (
        f"{TAB_ICONS['lmer']} is this view's word for a run's session, and the "
        "fleet card no longer uses it for the harness driving one"
    )
    # On the entry component since #274: the record composes it and draws no icon
    # of its own, so the shape the pane is being matched against is there.
    entry = _read(components / "AskEntry.vue")
    assert PANE_ICONS["chat"] in entry, (
        f"the operator-chat pane draws {PANE_ICONS['chat']} and the record it "
        "renders draws something else"
    )
    assert "<AskEntry" in _read(components / "AskHistory.vue"), (
        "the record draws its entries itself again, so the icon this test just "
        "read is not the one the pane shows"
    )
    # And the two conversations stay apart: reading what the session said and
    # replying to what it asked are different acts, and this is the view where the
    # two sit one tab from each other.
    assert PANE_ICONS["conversation"] != PANE_ICONS["chat"]


def test_neither_long_view_is_rendered_outside_its_own_pane():
    """That is what stacked looked like, and it would undo both the bounded box the
    emulator sizes itself from and the chunk that is deferred with the pane.

    ``AskChannel`` is deliberately not in this list any more (T40): the dock is
    short by construction — open questions and whatever has not been cleared — and
    it is above the tabs *because* a remembered tab must not be able to hide a
    session that is blocked. What must not be doubled is the record, which is the
    long one.
    """
    detail = _read(RUN_DETAIL)
    panes = _panels("pane")
    outside = detail
    for pane in panes.values():
        outside = outside.replace(pane, "")

    for component in ("<Chat", "<Terminal", "<AskHistory"):
        assert component not in outside, f"{component} is rendered twice — stacked again"
    assert detail.count("<AskChannel") == 1, (
        "the dock is rendered more than once, so a run with an open question polls "
        "its channel twice and offers two reply boxes for the same question"
    )


def test_the_terminal_pane_is_still_only_fetched_when_it_is_opened():
    """xterm is over half the JS and only this view opens one.

    Remembering "terminal" means the chunk is fetched when a run opens, which is the
    operator asking for it. What must not come back is the emulator in the entry
    bundle, or a pane that renders before it is selected — both are source-level
    decisions with no symptom in a build.
    """
    detail = _read(RUN_DETAIL)
    assert "defineAsyncComponent(() => import('./Terminal.vue'))" in detail
    assert "import Terminal from" not in detail, "back to one bundle"
    assert detail.count("import('./Terminal.vue')") == 1

    panes = _panels("pane")
    assert "eager" not in panes["terminal"], (
        "an eager pane renders before it is selected, so opening a run fetches the "
        "emulator again"
    )
    # The nesting adds a second chance to get this wrong: an eager *lmer* panel
    # renders the pane group, and with the terminal remembered that is the chunk.
    assert "eager" not in _panels("tab")["lmer"]


def test_the_operator_chat_pane_is_this_runs_channel_not_the_supervisor():
    """T31's supervisor chat is the whole fleet's and lives in a drawer.

    Putting it in a per-run tab would be a different feature wearing this one's
    label — and this pane has a session id in it precisely because what it shows
    belongs to one run.
    """
    chat = _panels("pane")["chat"]
    assert ':session-id="terminalSession"' in chat, "the channel is not keyed on a run"
    assert ':live="!!run.live"' in chat, "a record that cannot grow still polls"
    # No `@answered` here since T40, and that is the point of the split: this pane
    # is the record and answers nothing. The dock above the tabs is where a reply
    # is sent from, and it is the one that tells the fleet.
    assert "@answered" not in chat, (
        "the record takes replies, so there are two boxes for one question and the "
        "second is out of sight of the alert saying whether the session is alive"
    )
    detail = _read(RUN_DETAIL)
    dock = detail[detail.index("<AskChannel"):]
    dock = dock[:dock.index("/>") + 2]
    assert '@answered="emit(\'changed\')"' in dock, "a reply never reaches the fleet"

    for supervisor in ("Assistant", "supervisor chat"):
        assert supervisor not in detail, (
            f"{supervisor!r} in the run detail view: the supervisor chat is the "
            "fleet's, not a run's (T31)"
        )
    # "uber lmer" *is* in this view now, and in exactly one role: T85 badges the
    # header of the run that IS the orchestrator, which is a statement about which
    # run is being looked at rather than a second place to talk to it. So the word
    # is allowed where the badge is and nowhere a conversation could grow — a pane
    # that mentioned it would be the drawer arriving inside a run again.
    for pane in _panels("pane").values():
        assert "uber" not in pane, (
            "a session pane names uber lmer: the supervisor chat is the fleet's, "
            "not a run's (T31)"
        )
    assert "uber lmer" in _header(), (
        "the detail header no longer marks the orchestrator's own run (T85)"
    )


def test_the_related_runs_switcher_is_below_the_tabs_and_inside_no_panel():
    """The other thing a tab must not be able to hide, and the slot T66 left free.

    A switcher's whole point is switching *while looking at something else* (T53):
    in a tab it would be a tab you leave the terminal to visit in order to go
    somewhere else. So it sits after the tab window, at the end of the page — the
    one place below the bar that is behind nothing. Rendered inside a panel it would
    also be rendered once per panel, which is four fetches of the same list.
    """
    detail = _read(RUN_DETAIL)
    assert "<RelatedRuns" in detail, "the run detail view has no related-runs element"

    for panel in _panels("tab").values():
        assert "<RelatedRuns" not in panel, (
            "the switcher is inside a tab panel, so it is reachable only from the "
            "tab the operator happened to be on"
        )
    window = _window("tab")
    assert detail.index("<RelatedRuns") > detail.index(window) + len(window), (
        "the switcher is not below the tab window"
    )


def test_the_reason_a_run_needs_a_human_stays_out_of_the_tabs():
    """The price of remembering the tab, and the point of paying it here.

    This view no longer decides what the operator lands on: it can be the terminal,
    or the exit tab. So the state chip, the attention alert and the box that answers
    a question sit above the bar — a run that is waiting on a human must not be able
    to open on a pane that says nothing about it.
    """
    detail = _read(RUN_DETAIL)
    bar = detail.index('<v-tabs v-model="tab"')

    for above in ('v-if="run.attention"', "attentionLabel(", "<AnswerBox"):
        assert above in detail, f"{above} is gone from the detail view"
        assert detail.index(above) < bar, (
            f"{above} moved into a tab, so a remembered tab can hide it"
        )

    panels = _panels("tab")
    for panel in panels.values():
        assert "<AnswerBox" not in panel, "the answer box is behind a tab"


def test_the_alert_says_a_reply_is_wanted_without_repeating_the_question():
    """The operator asked: "the details page shows the is waiting on your reply full
    text in the alert, then again right below in the operation note, the alert can
    literally just be 'waiting for your reply'".

    For both question reasons the attention note *is* the question — the daemon
    puts the question's text on it — and the surface that can act on it renders the
    same paragraph a few lines below: the answer box for a run that stopped to ask,
    the channel dock for a live session. Two copies, and only the lower one has a
    reply box under it.

    The half worth guarding is what is *not* stripped: every other reason keeps its
    note, because a crash's note is the only account of a crash on this page. And
    the gate is the surface being present rather than the reason alone — a live
    question on a run with no session id renders no dock, and dropping the text
    then would leave the page asking for a reply and never saying what to.
    """
    header = _header()
    opening = re.search(r'<v-alert\b[^>]*v-if="run\.attention"', header, re.S)
    assert opening, "the detail view no longer says why the run needs a human"
    alert = _element(header, "v-alert", opening.start())

    assert "attentionLabel(run.attention.reason)" in alert, (
        "the alert lost the words that say what is wanted, which is the one thing "
        "it is left with"
    )
    assert 'v-if="run.attention.note && !noteRepeatedBelow"' in alert, (
        "the alert renders the note unconditionally again, so a question is shown "
        "twice — once where it can be answered and once where it cannot"
    )

    gate = _binding("noteRepeatedBelow")
    assert "questionPending" in gate and "askPending" in gate, (
        "the gate is not keyed on the two reasons whose note is a question, so it "
        "either hides a crash's note or repeats a question"
    )
    assert "terminalSession" in gate, (
        "a live question with no session renders no dock, and the alert would drop "
        "the only copy of the question on the page"
    )


# --- the header: what this run is (T61) --------------------------------------

def test_the_header_names_the_taskdef_the_repo_and_the_target():
    """The operator's ask, and what it buys.

    "i think i want target, repo name and taskdef to be in the header ...
    realistically i am not interested in overview or meta tabs most of the time
    (as long as i know the taskdef, target, and repo name)". Those three fields
    were the reason the overview got opened; in the header they are visible from
    whichever tab the operator was last on, which is the whole point — this view
    does not choose the tab any more.

    Pinned as *above the bar* rather than as present, because a copy of the same
    three fields lives in the overview and this file would pass on that alone.
    """
    header = _header()
    for field in ("{{ run.taskdef }}", "{{ run.project }}", "{{ targetLabel }}"):
        assert field in header, (
            f"{field} is not in the run detail header, so it is only reachable "
            "through a tab the operator did not choose"
        )
    # The target is rendered by a *pair* of branches since T85 — linked when it is a
    # URL, plain text when it is not — so what makes it present is that both are
    # keyed on it, not that either one is.
    assert 'v-else-if="run.target"' in header, (
        "the header shows a target only when it is a link, so a run whose target is "
        "a branch or a sentence no longer says what it is working on"
    )
    # The host, for the reason the fleet row carries it: two same-named projects on
    # two work-repo hosts are otherwise indistinguishable.
    assert "run.host" in _identity_line(), "the repo is named without its host"

    # A link, and one that leaves the app safely: the target is on another origin.
    link = re.search(r"<a\b[^>]*?>", _identity_line(), re.S)
    assert link, "the target in the header is text, not a way through to it"
    assert ':href="targetHref"' in link.group(0)
    assert 'rel="noopener"' in link.group(0) and 'target="_blank"' in link.group(0)


def test_the_header_reads_a_target_with_the_grammar_the_fleet_row_uses():
    """One parse of a target in this app, and it is not in a component.

    ``targetRef`` and ``shortTarget`` are in ``format.js`` because the fleet row
    needed exactly this vocabulary first (T50): a run is talked about as "MR #164",
    and the shortened URL is what a target that names no numbered resource reads as
    — a bare repo, a compare range, a taskdef whose target is prose. A second parse
    here would drift from that one silently, and the two views would name the same
    target differently.
    """
    detail = _read(RUN_DETAIL)

    assert {"targetRef", "shortTarget"} <= _format_imports(), (
        "the header does not take the target's words from format.js: it imports "
        f"{sorted(_format_imports())}"
    )
    assert "targetRef(props.run.target)" in detail, (
        "the header does not ask format.js what the target names"
    )
    for parsing in ("new URL(", ".pathname", "merge_requests", "split('/')"):
        assert parsing not in detail, (
            f"RunDetail.vue parses a target itself ({parsing}); that grammar lives "
            "in format.js, and a copy of it here is how the header and the fleet "
            "row start disagreeing about what a run points at"
        )

    # The same fallback the fleet row renders inline, in a computed because two
    # branches show it (T85: a target that is not a URL is not a link) and they must
    # not be able to name the target differently.
    assert (
        "resource.value ? resource.value.label : shortTarget(props.run.target)"
        in detail
    ), (
        "the header shows something other than the ref with the shortened target "
        "as its fallback — a target that names no resource would leave a hole "
        "where the run's link belongs"
    )
    assert _identity_line().count("{{ targetLabel }}") == 2, (
        "the linked and the unlinked target are not rendered from one label, so "
        "the two branches can drift into naming the same target differently"
    )


def test_a_long_target_scrolls_inside_the_header_instead_of_widening_the_page():
    """The header's one unbounded value, and an anchor is an inline element.

    ``shortTarget``'s output is a single unbreakable string, so it cannot wrap: on a
    phone it takes the page width with it and every view in the app scrolls
    sideways. ``.scroll-x`` is the treatment the rest of the app uses for targets,
    paths and session ids, and two of the three declarations that matter here are
    the non-obvious ones: ``display: block``, because overflow is ignored on an
    inline box, and ``min-width: 0``, because a flex child otherwise refuses to
    shrink below its content. They are asserted rather than assumed because this is
    the class's first inline user — everything else wearing it is a ``<code>`` or a
    ``<div>``.
    """
    identity = _identity_line()
    link = re.search(r"<a\b[^>]*?>", identity, re.S)
    assert link and "scroll-x" in link.group(0), (
        "the target in the header does not scroll inside its own box, so a long "
        "one pushes the whole page sideways on a phone"
    )

    rule = re.search(r"\.scroll-x \{(.*?)\}", _read(STYLE), re.S)
    assert rule, "style.css lost the .scroll-x rule the header depends on"
    for declaration in ("display: block", "overflow-x: auto", "min-width: 0"):
        assert declaration in rule.group(1), (
            f".scroll-x no longer declares {declaration}, which is one of the "
            "three that keep a long target inside its own box"
        )


def test_the_ports_a_run_published_are_reachable_from_whichever_tab_is_open():
    """A hosted service is the reason to open the run, not a fact about it.

    The chips lived in the overview's live-session card, which is the tab an
    operator watching a service is least likely to be on: reaching the thing the
    run built meant leaving the terminal for a panel whose facts they were not
    after. On the identity line they sit beside the taskdef and the target, in the
    region a remembered tab cannot hide.

    Moved rather than copied, which is the half only a source-level guard holds:
    two sets of the same links are two things to keep in step, and the shape they
    are drawn with is one edit from disagreeing.
    """
    identity = _identity_line()
    chip = re.search(r'<v-chip\b[^>]*v-for="port in run\.ports"[^>]*>', identity, re.S)
    assert chip, (
        "the ports this run published are not on the header's identity line, so "
        "opening a hosted service means finding the tab that lists them"
    )
    assert ':href="portUrl(port)"' in chip.group(0), (
        "the chip is not a way through to the service, or it builds the URL itself "
        "instead of asking format.js — which is where the fleet row's chips ask"
    )
    assert "portUrl" in _format_imports(), (
        "the header does not take the port's URL from format.js: it imports "
        f"{sorted(_format_imports())}"
    )
    for attribute in ('target="_blank"', 'rel="noopener"'):
        assert attribute in chip.group(0), (
            f"the port chip lost {attribute}; the service is another origin and "
            "this app is the tab the operator came back to"
        )

    # Nothing at all for a run with no ports, which is most of them: the daemon
    # reads `ports` off the live session's registry entry, so a run with nothing
    # running has none — and the `v-if="session"` the old site relied on is not up
    # here to say so.
    gate = re.search(r'<template v-if="run\.ports\?\.length">', identity)
    assert gate and gate.start() < chip.start(), (
        "the port chips are not gated on there being any, so a run with none "
        "renders an artifact on the line that names it"
    )

    for panel in _panels("tab").values():
        assert "run.ports" not in panel, (
            "a tab panel lists the ports too: the header copy is then the second "
            "of two, and the pair drifts the moment one of them is restyled"
        )


def test_the_header_stays_a_line_rather_than_becoming_a_card():
    """What the identity must not cost, and it is the reason it is in the header.

    Everything below it up here is what a run needs from a human — the state, the
    attention alert, the box that answers a question — and they are above the tabs
    precisely so a remembered tab cannot hide them. A card of key/value rows in the
    header hides them again, one screen lower, on the device this app is for.
    """
    header = _header()
    assert "<v-card" not in header, (
        "the header renders a card, which pushes the attention alert and the "
        "answer box down a phone screen — the identity is one wrapping line"
    )
    assert 'dl class="kv"' not in header, (
        "the header restates the overview's key/value grid; that grid is a column "
        "per row and this is a header"
    )

    identity = _identity_line()
    assert "<div" not in identity[1:], (
        "the identity is more than one row now, so it grows with every field "
        "somebody adds to it"
    )
    # The stacking, which is what makes three things above one tab bar readable
    # rather than a pile: the identity qualifies the name it sits under, and
    # nothing gets between the news a run needs a human and the box that answers
    # it.
    assert header.index("{{ run.taskdef }}") < header.index('v-if="run.attention"'), (
        "the identity is below the attention alert, so it separates the run's name "
        "from the line that describes it and the alert from the answer box"
    )
    assert header.index('v-if="run.attention"') < header.index("<AnswerBox")


def test_the_header_is_named_by_the_title_when_this_orchestrator_wrote_one():
    """The operator asked: "if meta.title is set it should be used in the details
    header".

    The heading line is what a run is *called*, so that is where the title goes —
    not an extra item on the identity line below, which is deliberately one row
    (see the test after this one) and would grow with every field added to it.

    The fallback is the half that has to hold: most runs have no title, and
    ``label`` — the recorded name, else the slug — is always there. A header
    reading ``run.title`` alone would be a blank heading on every undescribed run.
    """
    header = _header()
    assert header.count("{{ run.title || run.label }}") == 1, (
        "the detail header does not name the run by its title with the label as "
        "the fallback, so either the title is invisible outside its own tab or the "
        "heading is empty for every run nobody has described"
    )
    # Above the identity line, which qualifies the name rather than carrying it.
    assert header.index("{{ run.title || run.label }}") < header.index(
        "{{ run.taskdef }}"
    ), "the run's name is below the line that describes it"


def test_the_header_still_says_what_the_run_is_called_under_its_title():
    """One view with room for both, and this is it.

    A title is what the run is *about*; the label is what it is *called* — and the
    slug is how it is talked about in a work repo, a branch name and every ``lmer``
    command. Replacing it outright in the one view devoted to a single run would
    leave the run path in the overview tab as the only place it survived, behind a
    tab the operator did not choose.
    """
    header = _header()
    label = re.search(
        r'<span\s+v-if="run\.title"[^>]*>\{\{ run\.label \}\}', header, re.S,
    )
    assert label, (
        "the header shows the title instead of the run's own name rather than "
        "above it, so a titled run no longer says what it is called"
    )
    assert "text-medium-emphasis" in label.group(0), (
        "the label is as loud as the title it sits under; the title is the heading"
    )


def test_a_long_title_wraps_under_the_state_chip_rather_than_pushing_it_off():
    """120 characters of agent-written text, above the tabs, on a phone.

    The chip is what says a run is waiting on a human, and it is *before* the name
    on the flex line, so a long title wraps under it instead of shoving it out of
    view. ``.scroll-x`` is on the name for the reason it is on the target one line
    down: a title can be a single unbreakable string, and this header is above
    everything a run needs from a human.
    """
    header = _header()
    assert header.index("toneColor(meta.tone)") < header.index(
        "{{ run.title || run.label }}"
    ), "the name comes before the state chip, so a long title pushes the signal off"

    name = re.search(
        r"<span[^>]*>\{\{ run\.title \|\| run\.label \}\}", header, re.S,
    )
    assert name and "scroll-x" in name.group(0), (
        "the name in the header does not scroll inside its own box, so an "
        "unbreakable title takes the whole page sideways on a phone"
    )


def test_the_title_in_the_header_is_text_rather_than_markdown():
    """A heading is not a document, and this text is agent-written.

    ``RunMeta.vue`` — the tab that owns the field — renders the description through
    the one component allowed to turn text into markup and interpolates the title,
    because the daemon has already collapsed it to one line. The header does the
    same with the same string: a second render path for it would be a bold
    half-sentence in a heading.

    Narrowed to the heading line by T46, deliberately. The region above the tabs is
    no longer markup-free — the attention note *is* prose an agent wrote, and the
    reason a run needs a human is exactly the sentence that should not arrive as a
    wall of asterisks — so what is pinned is the pair of decisions that survived:
    the run's *name* is a label and stays interpolated, and anything rendered up
    here is rendered in the compact mode, because a heading, a list or a fence
    above the tab bar would push the alert and the answer box off a phone.
    """
    header = _header()
    heading = header[:header.index("{{ run.taskdef }}")]
    for rendering in ("<Markdown", "v-html", "innerHTML"):
        assert rendering not in heading, (
            f"the run's name is rendered as markup ({rendering}); the title is a "
            "label, and the daemon has already collapsed it to one line"
        )
    assert "innerHTML" not in header and "v-html" not in header, (
        "the header injects markup of its own instead of going through the one "
        "component that sanitises it"
    )
    for element in re.findall(r"<Markdown\b[^>]*>", header, re.S):
        assert re.search(r"\binline\b", element), (
            f"the header renders block markdown ({' '.join(element.split())}); a "
            "list or a fence above the tab bar pushes the answer box off a phone"
        )


def test_the_overview_tab_holds_the_runs_facts_and_the_verb_that_continues_it():
    """Resuming is here rather than in the exit tab, and not by accident.

    The exit tab is labelled for ending a session; finding the verb that *starts*
    one behind it is the kind of mislabel that makes an operator hesitate over the
    button they came to press. It is also this panel that carries the facts the
    decision is made from — a row reading "stopped for your review" is answered by
    the status, the goal and the plan directly above the form.
    """
    overview = _panels("tab")["overview"]

    assert 'dl class="kv"' in overview, "the run's facts left the overview tab"
    assert "run.taskdef" in overview and "run.goal" in overview
    assert 'v-if="session"' in overview, "the live session's own facts are not here"
    assert "session.log_path" in overview

    assert 'v-if="canResume"' in overview, "continuing the run is not in the overview"
    assert "continuing this run" in overview
    assert "recent events" in overview


def test_the_run_row_falls_back_to_the_runs_key_and_never_composes_a_path():
    """The last surface of the composed-path family (T90, finished in T96).

    ``rel_path`` arrives only for a directory something actually found — the daemon
    confirms a named run's dir by content, because the container renames it to
    ``runs/<slug>--<name>``. A row without one is a run nobody has located: tracked
    but unpushed, or a session whose first commit has not landed. What that row can
    still say truthfully is the run's *key*, and assembling ``runs/<slug>`` from it
    would name a directory that does not exist for most runs — the same guess the
    daemon stopped making.
    """
    overview = _without_comments(_panels("tab")["overview"])
    assert "{{ run.rel_path || runKey }}" in overview, (
        "the run row no longer shows the found directory, or its fallback is not "
        "the run's key"
    )

    key = _binding("runKey")
    for part in ("run.host", "run.project", "run.slug"):
        assert part in key, f"the key is not the run's identity: {part} is missing"

    code = _without_comments(_read(RUN_DETAIL))
    assert not re.search(r"runs/\$?\{", code), (
        "the view composes a run directory from a slug again; for a named run that "
        "path names nothing, and only whatever found the directory may quote one"
    )


def test_a_fact_the_run_has_not_recorded_renders_nothing_at_all():
    """The em dash was the one placeholder left in this grid, and it is a claim.

    Every other row here is gated on the value existing — no taskdef, no taskdef
    row — and status was not: a run in its first seconds, which has written no
    state at all, got ``status —``. That reads as "the platform looked and there is
    nothing", where the truth is that nothing has been written yet, and it is a row
    of a grid on a phone either way.

    The status and the stop reason are one value for the same reason the fleet
    row's ``committed`` is one: a run that recorded a stop reason and no status
    must not render a bare ``(question)``, and a separator spelled in the template
    outlives the part beside it.
    """
    overview = _without_comments(_panels("tab")["overview"])
    # In the grids, not in the prose: the copy on this panel uses em dashes as
    # punctuation, and a placeholder is a value in a row.
    for match in re.finditer(r'<dl class="kv">', overview):
        grid = _element(overview, "dl", match.start())
        assert "—" not in grid, (
            "a key/value row renders an em dash where a value is missing; absent "
            f"renders nothing: {grid}"
        )
    assert '<dt v-if="status">status</dt>' in overview, (
        "the status row is drawn whether or not the run has recorded one"
    )
    assert "{{ run.status }}" not in overview and "{{ run.stop_reason }}" not in overview, (
        "the two halves are rendered separately, so the row can show a stop reason "
        "with nothing in front of it"
    )

    detail = _read(RUN_DETAIL)
    binding = detail[detail.index("const status = computed("):]
    binding = binding[:binding.index("\n\n")]
    assert ".filter(Boolean)" in binding, (
        "a part that is absent still reaches the join, so the row renders a "
        "separator with nothing beside it"
    )
    assert "|| null" in binding, (
        "an empty join returns '' rather than null; the v-if would still be false, "
        "but the value the view tests is no longer the value it renders"
    )


def test_the_session_card_says_how_long_the_harness_has_been_quiet():
    """T95's reading, in the view the operator lands on from the row that has it.

    The state chip is derived with liveness first (spec D24), so it reads
    ``running`` from the moment a session starts until the moment it exits: a run
    that finished its work and is sitting at its prompt looks exactly like one that
    is working. The fleet row already says ``idle 22m``; without it here, checking
    whether the session is still doing anything means going back to the list.

    In the row's idiom, which is the part that is easy to lose: dimmed and small,
    no chip, no colour — this is a fact the chip's verdict does not contain, not a
    competing verdict — and nothing at all when there is no reading, which is
    ordinary (an unreachable container, an older image).
    """
    overview = _panels("tab")["overview"]
    assert "{{ idle }}" in overview, (
        "the session card does not say how long the harness has been quiet"
    )
    element = re.search(r'<div\b[^>]*v-if="idle"[^>]*>\{\{ idle \}\}</div>', overview, re.S)
    assert element, (
        "the idle reading is not one element rendering the whole label, so a word "
        "spelled in the template can outlive the value beside it"
    )
    for quiet in ("text-body-small", "text-medium-emphasis"):
        assert quiet in element.group(0), (
            f"the idle reading lost {quiet}, so it reads as loudly as the state"
        )
    assert "<v-chip" not in element.group(0), "the idle reading became a chip"
    assert "last_output_at" in element.group(0), (
        "there is no way to see what the reading was measured against"
    )

    detail = _read(RUN_DETAIL)
    binding = detail[detail.index("const idle = computed("):]
    binding = binding[:binding.index("\n})") + 3]
    assert "activity?.idle_seconds" in binding, (
        "the label no longer reads the daemon's measurement out of the session "
        "block (inventory.SESSION_FIELDS)"
    )
    assert "duration(" in binding, (
        "the span is formatted by something other than format.js's duration(), so "
        "this view and the fleet row can disagree about when minutes become hours"
    )
    for computed_here in ("ago(", "Date.", "props.now"):
        assert computed_here not in binding, (
            f"the idle label is computed with {computed_here} — against this "
            "device's clock rather than from the daemon's measurement"
        )


def test_the_exit_tab_holds_both_ending_verbs_and_is_never_empty():
    """T27's two verbs move here whole, including their inequality.

    And the tab has to say something when there is nothing to end, which is the
    common case: most runs in this list have no container behind them, and an empty
    panel reads as a broken one.
    """
    exit_panel = _panels("tab")["exit"]

    assert 'v-if="session && run.live"' in exit_panel
    assert ">wind down</v-btn>" in exit_panel
    assert ">exit now</v-btn>" in exit_panel
    assert "confirmingExit = true" in exit_panel, "the blunt verb lost its dialog"
    assert "forget this run" in exit_panel, "tracking left the tab that ends things"

    # The else branch, and it has to point somewhere: the verb for a run with no
    # session is in another tab now.
    assert re.search(r"<p v-else[^>]*>\s*Nothing is running", exit_panel), (
        "a run with no live session gets an empty exit tab"
    )
    assert "overview tab" in exit_panel


# --- what is remembered, and what a stale value must not do ------------------

def test_the_choice_is_stored_globally_rather_than_per_run():
    """The operator asked for "I prefer the terminal", not "this run opens on the
    terminal".

    A key built from the run would answer a question nobody asked and would still
    open the *next* run on the conversation, which is the complaint.
    """
    detail = _read(RUN_DETAIL)
    for name in ("RUN_TAB_STORAGE_KEY", "RUN_PANE_STORAGE_KEY"):
        value = _string_constant(detail, name)
        assert value.startswith("lmer."), (
            f"{name} = {value!r} is not namespaced, so it can collide with anything "
            "else served from this origin"
        )
    # Lower case, so the key constants (which are shouted) are not what matches:
    # anything reading `props.run` here is building a per-run key.
    for accessor in ("storedTab", "storedPane", "rememberTab", "rememberPane"):
        body = _function_body(detail, f"function {accessor}")
        assert "run" not in body, (
            f"{accessor} names the run, so the preference is per run"
        )


def test_every_stored_key_is_named_where_it_is_stored():
    """What keeps the storage allowlist able to see what this app stores.

    ``getItem(key)`` inside a shared helper tells a reviewer nothing, and it would
    let a fourth key in without anybody adding it to ALLOWED_STORAGE_KEYS. So the
    helper takes the read or the write to perform and never the key, and the
    constants stay at the access.
    """
    assert "localStorage" not in _read(PREFERENCES), (
        "preferences.js names browser storage. If it touches it, the key at the "
        "call site is a parameter name and the allowlist in "
        "test_platform_web_app.py is blind; even in prose it trips that guard, "
        "which requires every mention to be a readable call"
    )

    for source in (RUN_DETAIL, TERMINAL):
        text = _read(source)
        declared = set(re.findall(r"const (\w*STORAGE_KEY) = '", text))
        used = set(re.findall(r"localStorage\.\w+\((\w+)", text))
        assert used, f"{source.name} stores nothing any more"
        assert used <= declared, (
            f"{source.name} stores under {sorted(used - declared)}, which it does "
            "not declare"
        )
        assert declared <= ALLOWED_STORAGE_KEYS, (
            f"{source.name} declares {sorted(declared - ALLOWED_STORAGE_KEYS)}, "
            "which is not in ALLOWED_STORAGE_KEYS"
        )


def test_the_terminals_two_preferences_go_through_the_same_helper():
    """Three keys, one set of rules — or the one left behind is the one that trusts
    what it read, throws in private browsing, or forgets on every reload."""
    text = _read(TERMINAL)

    assert "from '../preferences.js'" in text
    for call in ("storedChoice(", "rememberChoice(", "storedFlag(", "rememberFlag("):
        assert call in text, f"the terminal does not use {call}"
    assert "try {" not in _function_body(text, "function storedHeightScale"), (
        "a second copy of the throw-tolerance, which is what the helper is for"
    )
    assert "ref(storedResizeOptIn())" in text, (
        "the fit switch resets on every terminal again"
    )


def test_a_disarm_the_platform_asked_for_is_not_remembered_as_a_choice():
    """The one real hazard in persisting that switch.

    Two status frames turn fitting off without the operator touching anything: a
    session whose supervisor cannot resize, and one whose PTY is going away. Storing
    those would leave every terminal afterwards unfitted — an 80-column TUI in a
    phone's 45 — with nothing on screen saying why, which is the failure the
    default-on test exists for.
    """
    text = _read(TERMINAL)

    assert text.count("rememberFlag(") == 1, (
        "the fit switch is written from more than one place"
    )
    switch = _function_body(text, "function setResizeOptIn")
    assert "rememberFlag(" in switch, "the operator's own choice is not remembered"

    for event in ("resize_unsupported", "resize_failed", "resize_deferred"):
        case = text[text.index(f"case '{event}':"):]
        case = case[:case.index("return")]
        # Both spellings of the same mistake: writing the flag from the handler, and
        # routing the handler through the switch — which is the tidier-looking one
        # and stores it just the same.
        for storing in ("rememberFlag", "setResizeOptIn("):
            assert storing not in case, (
                f"a {event} frame stores the switch as if the operator had set it"
            )


# --- executed: what happens to a value that no longer means anything ---------

def _remembered_choice_source():
    """RunDetail.vue's preference block, verbatim.

    Verbatim is the point: the interesting failure is the component trusting what it
    read, and a paraphrase in this file would keep passing while it did.
    """
    text = _read(RUN_DETAIL)
    start = text.index(CHOICE_START)
    return text[start:text.index(CHOICE_END, start)]


#: One Node run over the real block, against a storage that is by turns empty,
#: stale, hostile and broken. The assertions are in Python; the JS half only
#: observes and reports.
_PROBE = """
// A localStorage that can be swapped, and can refuse the way real ones refuse.
let store = null
globalThis.window = {
  get localStorage() {
    if (!store) throw new Error('access denied (cookies blocked)')
    return store
  },
}
const plain = (entries, faults = {}) => ({
  entries,
  getItem: (key) => {
    if (faults.read) throw new Error('read denied')
    return key in entries ? entries[key] : null
  },
  setItem: (key, value) => {
    if (faults.write) throw new Error('quota exceeded')
    entries[key] = String(value)
  },
})

%s

const seen = {}
const both = () => ({ tab: storedTab(), pane: storedPane() })

// Nothing stored yet: the first run this operator opens.
store = plain({})
seen.fresh = both()

// Their choice, made and read back the way a second run reads it.
rememberTab('lmer')
rememberPane('terminal')
seen.written = { ...store.entries }
seen.reopened = both()

// A tab that this build does not have — renamed here, or written by another
// version of the app against the same origin. This is the one that would be a
// blank run view.
store = plain({ ...seen.written })
for (const key of Object.keys(store.entries)) store.entries[key] = 'gone'
seen.stale = both()
store = plain({ ...seen.written })
for (const key of Object.keys(store.entries)) store.entries[key] = ''
seen.blank = both()

// Storage that throws instead of answering: private browsing, some webviews.
store = plain({}, { read: true })
seen.readThrows = both()
store = null
seen.accessThrows = both()

// A value the app could not read back must not be stored at all, and a storage
// that refuses the write must not reach the operator as an error.
store = plain({})
rememberTab('a-tab-nobody-defined')
seen.refused = { ...store.entries }
store = plain({}, { write: true })
try {
  rememberTab('exit')
  seen.writeThrew = false
} catch (exc) {
  seen.writeThrew = exc.message
}

console.log(JSON.stringify(seen))
"""

#: The same, for the two flag helpers — the terminal's fit switch. Its own reads
#: live in a component and are pinned by reading; what is executed here is the rule
#: they hand off to, where "never stored" and "turned off" are different answers.
_FLAG_PROBE = """
store = plain({})
const read = () => store.getItem('lmer.terminal.fitToScreen')
const write = (value) => store.setItem('lmer.terminal.fitToScreen', value)

seen.flags = {}
seen.flags.missing = storedFlag(read, true)
rememberFlag(write, false)
seen.flags.storedOff = { ...store.entries }
seen.flags.off = storedFlag(read, true)
rememberFlag(write, true)
seen.flags.on = storedFlag(read, true)
store = plain({ 'lmer.terminal.fitToScreen': 'yes please' })
seen.flags.garbage = storedFlag(read, true)
store = plain({}, { read: true })
seen.flags.throws = storedFlag(read, true)
"""


def _probe():
    """Run the component's own preference block under Node and report what it did.

    Missing Node skips, unless the host says it has one
    (:func:`tests.conftest.require_node_toolchain`): a guard that can be satisfied
    by not running is the bug that helper exists to catch.
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    script = "\n".join([
        f"import {{ rememberChoice, rememberFlag, storedChoice, storedFlag }} "
        f"from {json.dumps(str(PREFERENCES))}",
        _PROBE % _remembered_choice_source(),
    ])
    # The flag half runs in the same process, appended before the one print so both
    # halves cost a single Node start.
    script = script.replace(
        "console.log(JSON.stringify(seen))",
        _FLAG_PROBE + "\nconsole.log(JSON.stringify(seen))",
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(WEB), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the preference probe failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_a_first_visit_lands_on_the_defaults():
    """The first entry of each list, and the same two views as before this slice."""
    seen = _probe()

    assert seen["fresh"] == {"tab": "overview", "pane": "conversation"}, (
        f"a first visit opens on {seen['fresh']}"
    )


def test_the_choice_survives_leaving_the_run_and_reloading_the_app():
    """The complaint, restated as the thing that must work: the terminal is what
    opens next time, in this run and in every other.

    Both halves are here because either alone is a preference that does not work.
    The probe exercises the read and the write; the wiring is read from the source,
    since nothing executed can tell that the component never calls them — which is
    what a remembered choice that is never stored looks like.
    """
    seen = _probe()
    detail = _read(RUN_DETAIL)

    assert seen["reopened"] == {"tab": "lmer", "pane": "terminal"}, (
        f"a second visit opens on {seen['reopened']}"
    )
    assert set(seen["written"]) == {
        _string_constant(detail, "RUN_TAB_STORAGE_KEY"),
        _string_constant(detail, "RUN_PANE_STORAGE_KEY"),
    }, f"stored under {sorted(seen['written'])}"

    assert "const tab = ref(storedTab())" in detail, "the view does not open on it"
    assert "const pane = ref(storedPane())" in detail
    for level, writer in (("tab", "rememberTab"), ("pane", "rememberPane")):
        assert re.search(rf"watch\({level}, \(value\) => {writer}\(value\)\)", detail), (
            f"nothing writes the {level} down, so it is remembered until the page "
            "is closed and never after it"
        )


def test_a_tab_that_no_longer_exists_falls_back_instead_of_blanking_the_view():
    """The failure this validation exists for.

    Vuetify renders nothing at all for a model value that selects no panel, so a
    renamed tab, a dropped one, or another version of this app writing the same key
    would leave an operator looking at a run and seeing an empty page — with a
    preference they cannot find to clear.
    """
    seen = _probe()

    assert seen["stale"] == {"tab": "overview", "pane": "conversation"}, (
        f"a tab name this build does not have selected {seen['stale']}"
    )
    assert seen["blank"] == {"tab": "overview", "pane": "conversation"}, (
        f"an empty stored value selected {seen['blank']}"
    )


def test_a_storage_that_refuses_is_not_a_broken_run_view():
    """Private browsing and some embedded webviews throw on access rather than
    answering, and a full or disabled store throws on write. A preference is never
    worth failing to render over, and never worth an error the operator has to
    read."""
    seen = _probe()

    assert seen["readThrows"] == {"tab": "overview", "pane": "conversation"}, (
        f"a storage that refuses to be read selected {seen['readThrows']}"
    )
    assert seen["accessThrows"] == {"tab": "overview", "pane": "conversation"}, (
        f"a storage that refuses to be reached selected {seen['accessThrows']}"
    )
    assert seen["writeThrew"] is False, (
        f"a refused write reached the operator: {seen['writeThrew']}"
    )


def test_nothing_is_stored_that_could_not_be_read_back():
    """Otherwise it is read once, rejected, and quietly reset — which does not look
    like a stale value, it looks like the preference not being remembered."""
    seen = _probe()

    assert seen["refused"] == {}, (
        f"a tab this build does not have was stored anyway: {seen['refused']}"
    )


def test_a_remembered_flag_tells_never_stored_from_turned_off():
    """The fit switch's default is on, and "off" has to survive being read back.

    Treating a missing key and a stored ``false`` the same way is the natural
    mistake here, and it makes the switch unstorable: every reload turns fitting
    back on and reflows a terminal somebody else is watching.
    """
    flags = _probe()["flags"]

    assert flags["missing"] is True, "nothing stored must mean the default"
    assert flags["off"] is False, "turning it off is not remembered"
    assert flags["on"] is True
    assert flags["garbage"] is True, "a value neither end wrote must fall back"
    assert flags["throws"] is True
    assert list(flags["storedOff"].values()) == ["off"], (
        f"a flag is stored as {flags['storedOff']}, which is not readable at a "
        "glance in a devtools storage pane"
    )


# --- which run this is: the orchestrator's own (T85) --------------------------

def test_the_header_marks_the_orchestrators_own_run():
    """The operator asked: "clearly marked that it is that, both in the list and the
    detail".

    The detail view is where the marking matters most, because it is the view with
    the verbs on it: "wind down" and "exit" read very differently once you know the
    session you are ending is the one orchestrating everything else. So it goes on
    the heading line beside the state chip — above the tabs, which is the region a
    remembered tab cannot hide — in the drawer's words, icon and accent, because it
    is the same thing named in two places.
    """
    header = _header()
    badge = re.search(r"<v-chip\b[^>]*v-if=\"run\.orchestrator\"[^>]*>", header, re.S)
    assert badge, (
        "the detail header does not mark the run that IS the orchestrator, so the "
        "one session an operator must not casually end looks like all the others"
    )
    assert 'color="primary"' in badge.group(0), "the badge is not the accent"
    assert "mdiRobot" in badge.group(0), (
        "the badge draws a different shape from the drawer that opens the same "
        "session, so nothing connects the two"
    )
    assert ">uber lmer<" in header, "the badge has no word, only a colour and a shape"
    # Beside the signal and before the name, on the line that already carries both:
    # the identity line below is deliberately one row and is about the *run*.
    assert (
        header.index("toneColor(meta.tone)")
        < header.index(">uber lmer<")
        < header.index("{{ run.title || run.label }}")
    ), "the badge is not between the state chip and the run's name"


def test_a_run_that_is_not_the_orchestrator_renders_as_it_did_before():
    """One row in a fleet is the orchestrator; every other view of this page is not.

    The flag is read exactly once — in the badge's own ``v-if`` — so nothing else in
    the view can be keyed on it: no tab that appears, no verb that disappears, no
    tone. Which also keeps the two halves of T85 apart: marking the row is a label,
    and what an operator is allowed to do to that session is not this slice.
    """
    detail = _read(RUN_DETAIL)
    assert detail.count("run.orchestrator") == 1, (
        f"the view reads the flag {detail.count('run.orchestrator')} times; only "
        "the badge may depend on it"
    )
    for panel in _panels("tab").values():
        assert "run.orchestrator" not in panel, (
            "a tab panel branches on the row being the orchestrator's; the badge is "
            "a label, and the verbs behind the tabs are unchanged by it"
        )


# --- a target is a link only when it is one (T85) -----------------------------

def test_a_target_that_is_not_a_url_is_named_but_not_linked():
    """The operator asked: "its target does link to a 404 currently ... probably
    nothing to link".

    T61 rendered the header's target as ``<a :href="run.target">`` unconditionally,
    and most of what a taskdef can be handed as a target is not a URL: a branch
    name, a sentence, the orchestrator's own ``fleet``. Those became *relative*
    links — resolved against whatever path the app is served from — so every one of
    them 404'd. The words stay either way, in both places this view shows a target;
    it is the anchor that is conditional, and the link icon goes with it.
    """
    identity = _identity_line()
    anchors = re.findall(r"<a\b[^>]*?>", identity, re.S)
    assert len(anchors) == 1, f"the header has {len(anchors)} target anchors"
    assert 'v-if="targetHref"' in anchors[0], (
        "the header's target anchor is unconditional, so a target that is not a URL "
        "is a relative link that 404s"
    )

    fallback = re.search(r"<span\b[^>]*v-else-if=\"run\.target\"[^>]*>", identity, re.S)
    assert fallback, (
        "a target that cannot be linked is not rendered at all, so the header stops "
        "saying what the run is working on"
    )
    for attribute in ("href", "target=", "rel="):
        assert attribute not in fallback.group(0), (
            f"the unlinked target carries {attribute}, which is the anchor arriving "
            "again in a different tag"
        )

    # The overview tab shows the whole URL rather than the ref, which makes a dead
    # relative link there even easier to read as a real one. Same gate, same helper.
    overview = _panels("tab")["overview"]
    assert '<a\n                  v-if="targetHref"' in overview, (
        "the overview's target row still links whatever the target is"
    )
    assert "<template v-else>{{ run.target }}</template>" in overview, (
        "the overview drops the target instead of naming it unlinked"
    )


def test_the_link_gate_is_format_js_and_not_a_second_test_in_the_view():
    """One definition of "is this a link", for the reason there is one target parse.

    ``format.js`` already owns the *label* side of a target — which resource it
    names, how it is shortened — and the href side has to answer from the same fact
    or the header and the fleet row disagree about what is clickable. A regex here
    would also be the second place to get ``mailto:`` and ``javascript:`` wrong.
    """
    detail = _read(RUN_DETAIL)
    assert "targetLink" in _format_imports(), (
        "the view does not take the link gate from format.js: it imports "
        f"{sorted(_format_imports())}"
    )
    assert "targetLink(props.run.target)" in detail, (
        "the view has a target gate that is not the shared one"
    )
    stripped = _without_comments(detail)
    for grammar in ("new URL(", ".protocol", "startsWith"):
        assert grammar not in stripped, (
            f"RunDetail.vue decides for itself what a link is ({grammar})"
        )
