---
name: chrome-agent
description: Drive a real Chrome from inside the lmer container over the Chrome DevTools Protocol — read pages, fill forms, call same-origin APIs as the logged-in user, watch network and console traffic. Use whenever a task needs a browser rather than an HTTP client: a page that renders its content in JavaScript, a flow behind a login, a form or checkout to complete, a deploy to confirm visually, or a site that ignores scripted input. Also the skill to read when scripted clicks land with no effect, when a page behaves differently for you than for a human, or when you need to decide between a screenshot and a structured read.
---

# chrome-agent

`chrome-agent` is [captivus/chrome-agent](https://github.com/captivus/chrome-agent) — a
thin CLI that speaks raw Chrome DevTools Protocol. Every command is
`chrome-agent [<instance>] Domain.method '{json}'`, sent straight to Chrome with nothing
validated against a bundled schema, so protocol surface newer than the tool's own build
works anyway.

That is the whole point and the whole hazard: there is no curated verb layer to keep you
out of trouble, so **the discipline lives in this document**. Read it before you drive
anything.

The tool ships its own manual — `chrome-agent guide` prints it, `chrome-agent guide --path`
locates it. Read it when you need the mechanics; this skill is the method. (Its "Further
reading" section points at files in the upstream checkout, which a packaged install does
not carry — ignore those paths.)

## Start and stop

```bash
chrome-agent-launch --headless        # --headless maps to Chrome's --headless=new
# -> {"name": "workspace-01", "port": 9222, "pid": 12345, ...}
chrome-agent status                   # instances and their tabs
chrome-agent stop workspace-01        # ALWAYS, as soon as you are done
```

**Launch through `chrome-agent-launch`, not `chrome-agent launch`.** It is a thin wrapper
that runs the real thing, passes its arguments and output through untouched, and records
the instance as *this session's*. Everything else is plain `chrome-agent`.

That record is the only thing that makes automatic cleanup safe. captivus's own registry is
container-global and its instance names come from the working directory — every session in
`/workspace` produces `workspace-NN` — so nothing in it says who owns what. A browser
started with a bare `chrome-agent launch` is therefore **never** swept: not an error, just
yours to stop by hand.

Headless is the default and should stay it. See [Headed](#headed-when-behaviour-is-scored)
for the exception and [Memory](#memory-the-binding-constraint) for why `stop` is not
optional.

## The loop: sense, act, sense again

An agent does two things in a loop. **Sensing is the continuous default; acting is the
intermittent intervention.** After you act you are sensing again, and that perception is
both your confirmation of the act and your orientation for the next one.

**Never trust an act's return value — trust the next sense.** A CDP call that returns
without an error has told you the *message was accepted*, not that anything happened. Only
an observed effect proves an effect. And verify through a *different* channel than the one
you acted on: if you clicked, do not ask the click — read the DOM, watch the network, or
look at the screen.

Two sensing channels, matched to the question:

- **What the page *says*** — `DOM`, `Accessibility`, `CSS`, `DOMSnapshot`, and
  `Runtime.evaluate` reading `innerText`/`textContent`. This is the primary channel for
  content, structure and state.
- **What the page *reports*** — `Network` (what it fetched), `Runtime` (console,
  exceptions), `Log`, `Audits`.

A **screenshot** (`Page.captureScreenshot`) answers what the page *looks* like — layout, an
image, a CAPTCHA tile grid. It is the last resort for reading *content*: pixels are lossy.
Derive identifiers from data, never from pixels.

Sense readiness before you act. After `Page.navigate`, poll for an observable condition
rather than sleeping a fixed interval:

```bash
chrome-agent workspace-01 Runtime.evaluate \
  '{"expression":"document.readyState","returnByValue":true}'   # until "complete"
```

## The ladder: climb only as high as the page forces you

When the loop will not close — you sense, you act, you sense again, and the page still has
not done what you asked — that is the page fighting back, and the answer is to climb one
rung, not to debug your selector.

Three rungs, cheapest first. **Stop at the lowest one that works.**

### Rung 1 — don't act human at all

Synthetic events and in-page JavaScript. Free, instant, and the right default for any page
that does not check where its input came from.

```bash
# click
chrome-agent workspace-01 Runtime.evaluate \
  '{"expression":"document.querySelector(\"#submit\").click()"}'
# read
chrome-agent workspace-01 Runtime.evaluate \
  '{"expression":"document.body.innerText.slice(0,2000)","returnByValue":true}'
```

Often you should not touch the UI at all. `Runtime.evaluate` running `fetch()` *inside* the
logged-in page inherits its session, which makes the browser an authenticated HTTP client
with no credential handling on your side — pass `awaitPromise:true` and
`returnByValue:true` or you get a handle back before the data resolves.
`performance.getEntriesByType("resource")` recovers the endpoints the page already called,
which beats guessing at an API.

### Rung 2 — trusted input

Climb here when a Rung-1 click **silently no-ops**: no error, no complaint, nothing
happens. That signature — not a thrown exception — is the tell. Chrome stamps every event
with `isTrusted`, a click fired from page JS is stamped `isTrusted: false`, and pages that
care simply drop it.

CDP's `Input` domain enters Chrome's native input pipeline at the compositor, the same path
your own mouse takes, so the page cannot tell the difference. It also reaches things
synthetic clicks cannot: cross-origin iframes, capture-phase overlays, shadow-DOM overlays.

```bash
# Sense: centre coordinates (use the OUTER element; inner nodes can return {0,0,0,0})
chrome-agent workspace-01 Runtime.evaluate '{"expression":"(()=>{const r=document.querySelector(\"#submit\").getBoundingClientRect();return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};})()","returnByValue":true}'
# Act: a real click is press + release
chrome-agent workspace-01 Input.dispatchMouseEvent '{"type":"mousePressed","x":400,"y":300,"button":"left","clickCount":1}'
chrome-agent workspace-01 Input.dispatchMouseEvent '{"type":"mouseReleased","x":400,"y":300,"button":"left","clickCount":1}'
# Sense again, through an independent channel
chrome-agent workspace-01 Runtime.evaluate '{"expression":"document.querySelector(\"#result\").textContent","returnByValue":true}'
```

Typing is `Input.insertText` or `Input.dispatchKeyEvent`. React-controlled inputs need the
native value setter so React notices the change (see `chrome-agent guide`).

Trusted `Input` coordinates are **viewport-relative**, and Chrome routes the event to
whatever sits under them — including inside a cross-origin iframe, with no iframe-relative
maths. That is how you reach a control you cannot select: ask where the iframe is, compute
the point, and click the glass.

**Rung 2 is orthogonal to headless vs headed.** CDP input is trusted *in headless mode*.
Do not reach for a headed browser to fix an untrusted-input problem — see
[Two orthogonal blockers](#two-orthogonal-blockers).

### Rung 3 — human behaviour

Climb here only when the page scores *behaviour* rather than input trust: drag CAPTCHAs
that sample the mouse trail, risk engines that model movement, challenges that need eyes.

- **Vision** — screenshot the region and read it, for anything only a picture answers
  (tile grids, distorted text, "where is the gap").
- **Human-shaped motion** — a drag that eases in, curves slightly, overshoots a little and
  settles, dispatched as a sequence of `Input.dispatchMouseEvent` moves. A straight-line
  instant drag is a signature.
- **Dwell** — real people pause between locating and clicking.

This rung is expensive and slow. It earns its place only when Rungs 1 and 2 have both
demonstrably failed.

### Then write it down

Explore with the loop until the thing works, then record the rung and the sequence that
worked — as a script, as notes on the task, or as an addition to a skill. The value is in
never rediscovering it: figure it out once, do it forever.

## Two orthogonal blockers

When a flow fails, decide *which* problem you have, because they are separately fixable and
the fix for one does nothing for the other:

| blocker | symptom | fix |
|---|---|---|
| **Untrusted events** | a scripted click silently no-ops; the page behaves as if nothing was clicked | Rung 2 — trusted `Input`. Works headless. |
| **Headless risk score** | everything "works" but the site rates you badly: extra challenges, a declined transaction, a soft block | headed via Xvfb, plus Rung 3 behaviour. Trusted input does not help. |

Diagnosing which one you are looking at is most of the work. Reaching for headed mode
because a click did not register wastes the memory budget and does not fix the click.

## The CAPTCHA split: code where you can, model where you must

If you ever have to work a challenge, split the machine in two and keep the split strict:

- **Deterministic code** owns the trusted checkbox click, piercing into the challenge
  iframe, the per-round screenshot of the tile grid, and re-arming when a round expires.
  This half is fast, free, and repeatable.
- **The model** is called for exactly one thing: looking at the picture and saying which
  tiles match. Nothing else.

**Minimise round-trips — challenges expire.** Every avoidable model call is time on a clock
you do not control, and a round that times out costs you the whole attempt. Batch the
screenshot and the question into one call; do not narrate.

## Memory: the binding constraint

The container's memory limit is 2048 MiB by default (`LMER_MEMORY` changes it per run —
read your own from `/sys/fs/cgroup/memory.max`), and a session's baseline is already
~1100 MiB. **Each headless Chrome costs roughly 240–320 MiB**, so about **two** fit, and a
heavy SPA can eat the second.

Those are three measurements, not a rule: +239 MiB in this image (1288 → 1527, and `stop`
gave the 239 back), and +263 then +315 MiB for two instances in the evaluation that chose
this tool — where a third never finished starting before the cap.

captivus keeps Chrome alive between CLI calls by design — that is what makes it fast, and
it is also what makes a forgotten instance a ~300 MiB leak for the rest of the run.

- **One browser per task.** Reuse the instance; do not launch per step.
- **`chrome-agent stop <instance>` the moment you are finished**, not at the end of the
  session. Treat it like closing a file handle.
- `chrome-agent cleanup` prunes instances that are already **dead**. It does not stop a
  live browser, so it is not a substitute for `stop`.
- A `SessionEnd` hook stops the instances **this session launched through
  `chrome-agent-launch`** — nothing else, and nothing anyone else started. It re-checks
  that each one is still the same process before stopping it, because captivus reuses
  instance names, and leaves anything it cannot confirm. Treat it as
  housekeeping, not as cover: it fires when the session ends, far too late to help the run
  that leaked, Claude does not block its exit on SessionEnd hooks so teardown can kill it
  mid-sweep, and it deliberately leaves any browser it cannot attribute to you. It earns
  its place only where the container outlives the session.

## Token cost: never dump the accessibility tree

`Accessibility.getFullAXTree` on an ordinary page returned **1,146,534 bytes** in a single
call — enough to blow a context window outright. The raw protocol will hand you a megabyte
without comment; only this rule stops it.

Read narrowly, in rough order of preference:

```bash
# scoped text — the usual right answer
chrome-agent workspace-01 Runtime.evaluate \
  '{"expression":"document.querySelector(\"main\").innerText.slice(0,4000)","returnByValue":true}'
# structured, scoped
chrome-agent workspace-01 Runtime.evaluate \
  '{"expression":"[...document.querySelectorAll(\".row\")].slice(0,30).map(e=>e.innerText)","returnByValue":true}'
```

Whole-page `document.body.innerText` is ~4 KB on a typical page and is fine as a fallback.
Reach for `Accessibility` only for a *specific* node (`Accessibility.getPartialAXTree` with
a `backendNodeId`), never for the full tree. Always `.slice()` a bound onto anything you do
not control the size of.

## Headed: when behaviour is scored

Headless is the default. The image also carries Xvfb, so a headed browser is available for
flows where the *score* matters (see the table above) rather than for anything that is
merely about input trust:

```bash
Xvfb :99 -screen 0 1920x1080x24 &                     # survives; a background process does
DISPLAY=:99 chrome-agent-launch                       # no --headless: a real headed Chrome
DISPLAY=:99 chrome-agent workspace-01 Page.navigate '{"url":"https://example.com"}'
```

**Put `DISPLAY=` on each command, not in an `export`.** A shell variable does not survive
between tool calls — only the backgrounded `Xvfb` does — so an `export DISPLAY=:99` in one
call is gone by the next, and the launch then fails on a missing X server. That reads as
"headed is broken here" rather than "the variable was dropped". (Exporting it once is fine
if the whole recipe runs in a single invocation.)

`xdotool` is installed for captivus's headed desktop-pinning path. Headed costs the same
few hundred MiB as headless, plus the X server.

**Two things to stop, not one.** `chrome-agent stop <instance>` ends the browser; nothing
ends `Xvfb`. Kill it yourself when the flow is done (`kill %1`, or by pid) — the
`SessionEnd` sweep only knows about chrome-agent instances and will leave an X server
running.

## Gotchas

- **Navigation destroys context.** A pending `Runtime.evaluate` fails with "context
  destroyed" when the page navigates. Retry against the new page.
- **`nodeId`/`objectId` go stale between one-shot calls.** `backendNodeId` is the handle
  that survives — use it for `DOM.setFileInputFiles` and friends.
- **Shadow roots and cross-origin iframes** are invisible to a main-frame `querySelector`.
  `DOM.getDocument '{"depth":-1,"pierce":true}'` sees them;
  `DOM.getNodeForLocation` finds what is under a coordinate.
- **Several live instances** disable name auto-selection for bare one-shot methods — name
  the instance.
- **Every one-shot call costs ~1.5 s of Python start** in this image (measured:
  1.3–1.7 s warm, ~7 s on the first call of a session; upstream's README quotes
  ~70 ms, which excludes process start). A ten-step interaction is a quarter of a
  minute in process startup alone. For tight loops or event capture, use
  `chrome-agent attach` or the Python API rather than a shell loop.
- **Egress is open and the browser carries cookies.** A page you drive can reach anything
  this container can reach, runs its JavaScript, and (per captivus's own note) WebRTC can
  leak the real IP regardless of fingerprint profile. Drive sites you are authorised to
  drive.

## The name

`chrome-agent` here is **captivus/chrome-agent** (PyPI `chrome-agent`), unwrapped and
unrenamed — `chrome-agent-launch` is an lmer helper beside it, not a replacement for it. A different project
— [sderosiaux/chrome-agent](https://github.com/sderosiaux/chrome-agent), a Rust CDP tool
with a curated verb layer — installs an executable of *literally the same name*. They
cannot coexist under one verb: on `PATH` one silently wins.

If a second CDP tool is ever added to this image, **it** gets the distinct name or the
wrapper. `chrome-agent` means captivus. Do not assume both are reachable under this verb,
and do not "fix" a missing verb by installing the other one over the top.

## Sources

- Corey J. Gallon, *The Dark Arts of Web Automation* —
  <https://gallon.me/the-dark-arts-of-web-automation.html>. The ladder, the loop, and the
  CAPTCHA split come from this talk; the author also wrote captivus/chrome-agent.
- captivus `AGENTS.md`, shipped inside the package: `chrome-agent guide`. The CDP mechanics,
  the sense-channel taxonomy, and the recipes beyond driving the UI.
- The memory, latency and output-size figures are lmer's own measurements in this image.
