<script setup>
// Text an agent wrote, turned into markup (T38, shared by T44).
//
// Four views show words that were produced *inside a container*: the
// conversation (Chat.vue), the operator channel's feed (AskChannel.vue) and the
// question a live session is blocked on (AskBox.vue). Shown verbatim, all three
// are a wall of asterisks and backticks, because what an agent writes is markdown
// in practice — fenced commands, lists, tables.
//
// Why it is one component and not a helper each view calls
// -------------------------------------------------------
// This is the only place in the UI where untrusted text becomes markup, so the
// number of implementations of it is a security property in itself: a second
// hand-rolled copy is exactly how one of the three ends up without the sanitiser,
// and nothing on screen would say so. A component is a stronger seam than a
// shared function for that, in two ways a function cannot manage:
//
//   - there is one `v-html` in the whole app and it is the line below. A helper
//     would leave three of them, each one an edit away from being bound to the
//     raw text instead;
//   - half of what makes rendered output safe to read on a phone is CSS — above
//     all that a fence scrolls sideways and never reflows, because the operator's
//     use for one is to paste it into a shell. Those rules live with the markup
//     they style, so a new consumer cannot get the renderer and miss them.
//
// The two layers, which fail independently
// ----------------------------------------
//   1. the parser is never allowed to produce HTML. `html: false` is markdown-it's
//      documented safe setting: a `<script>` in the text comes out as the
//      characters of one, because it was never read as a tag to begin with.
//   2. what the parser does produce is checked against an allowlist of tags and
//      attributes before it reaches v-html — so a hole in layer 1 still has to
//      get past something that has never heard of the tag it wants.
//
// Both are bundled, for the reason main.js gives for choosing SVG icon paths over
// the MDI webfont: nothing is fetched at runtime, on a LAN that may have no route
// out. That reasoning is also why images are off — `![](…)` would otherwise make
// this browser issue a request to whatever host a container's output named, which
// is a beacon, a blank box, and (arriving late) a message that changes height
// after the view has already scrolled to the end of it.
//
// Two shapes of the same path, and why the second one exists (T46)
// -----------------------------------------------------------------
// Agent prose is not only in bubbles. It is also the attention note on a run, the
// clause a session closed a question with, and the note on an event-log line —
// one-line surfaces, read at a glance, several to a card. Those carry markdown too
// (a path in backticks, an emphasised word), and verbatim they are the same wall of
// punctuation the conversation was.
//
// What they cannot carry is a *block*. A heading, a list or a fence in a row makes
// the row three rows tall and stops it being scannable, and a <pre> in a flex line
// takes the page sideways on a phone. So `inline` is a second parse rather than a
// second renderer: markdown-it's `renderInline` never builds a block construct at
// all, so `# heading` and `- item` stay the characters the agent typed and only
// emphasis, code spans and links become markup. Everything that makes the output
// safe is shared, because it is the same parser instance: `html: false`, the link
// allowlist, the image ban, and a sanitiser pass over what comes out.
//
// One-line *titles* are not this (T65): a title is a label the daemon has already
// collapsed, and a listing renders it as text. Inline mode is for prose that
// happens to be short, not for every short string.
//
// Fetched when a view first renders one, not with the app (T42)
// -------------------------------------------------------------
// Being one module is also what makes the renderer deferrable, so every consumer
// imports this file with `defineAsyncComponent` and both packages leave the entry
// bundle in a chunk of their own — the same move RunDetail.vue makes for the
// terminal, for the same reason. The landing screen is the fleet view: a list of
// runs, a poll, and no markdown on it anywhere. It was paying 59 kB gzipped for
// this (app.js 208.1 → 149.0 kB gzipped, plus the rules below leaving the entry
// stylesheet for Markdown.css) to render nothing.
//
// Nothing is drawn while the chunk is on its way, deliberately: the loader form of
// `defineAsyncComponent` has no loading component, and the stand-in anybody would
// write for *this* text is the second render path the whole file exists to
// prevent. What that costs is one round trip in which a turn has its header and no
// body, which Chat.vue handles where it shows — a conversation that was following
// the end has to still be following it when the words arrive.
//
// The block between the two markers below is lifted verbatim by
// tests/test_platform_web_markdown.py and run against hostile input under Node, so
// it stays free of anything that needs Vue or a DOM.

import { computed } from 'vue'
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'

// Imported, never restated: the shape both layers admit has to be the one the
// dispatcher parses, or a reference renders as a live link and then refuses to
// resolve (issue #241).
import { RUN_REF_RE, dispatchRunRef, parseRunRef } from '../runref.js'

// --- render path (extracted by tests/test_platform_web_markdown.py) ----------
// Deny by default. The difference between this and a list of the schemes known to
// be dangerous is that the next dangerous one is already covered.
// Case in the pattern rather than an `i` flag: ALLOWED_URI_REGEXP below combines
// this source with RUN_REF_RE's, and a flag there would apply to both halves,
// admitting `LMER://RUN/…` — which the parser and the click handler both refuse.
const SAFE_LINK = /^(?:[Hh][Tt][Tt][Pp][Ss]?|[Mm][Aa][Ii][Ll][Tt][Oo]):/

// A run reference is admitted beside the web schemes and only as a whole, which
// is what keeps the scheme from being an escape hatch out of the allowlist: one
// shape, and it names a view.
const LINK_OK = (url) => {
  const raw = url.trim()
  return SAFE_LINK.test(raw) || RUN_REF_RE.test(raw)
}

const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  // Off deliberately: typographer rewrites `--` and `...`, and this text is full
  // of shell flags and paths that have to survive being read off the screen and
  // typed back in.
  typographer: false,
  // Agent output carries its own newlines and always did, so a single one is a
  // line break rather than the space CommonMark collapses it to. Without this,
  // rendering would *lose* formatting the plain view already had.
  breaks: true,
})

// The one markdown construct that fetches something by itself. Disabled at the
// parser rather than stripped afterwards, so there is no arrangement of layers in
// which an <img> gets built at all.
markdown.disable(['image'])

markdown.validateLink = LINK_OK

// A link the agent wrote leaves this page and cannot reach back into it:
// `noopener` is the one that matters (window.opener is a live handle on this
// document), `noreferrer` keeps the run's URL out of the other end's logs.
const renderLinkOpen = markdown.renderer.rules.link_open
  || ((tokens, index, options, env, self) => self.renderToken(tokens, index, options, env, self))
markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  // Not for a run reference: `_blank` on a scheme the browser cannot open is a
  // blank tab, and the click handler keeps that link on this page anyway.
  if (!RUN_REF_RE.test((tokens[index].attrGet('href') || '').trim())) {
    tokens[index].attrSet('target', '_blank')
    tokens[index].attrSet('rel', 'noopener noreferrer')
  }
  return renderLinkOpen(tokens, index, options, env, self)
}

// Layer 2, as an allowlist: everything markdown-it is meant to emit and nothing
// else. `class` is left out on purpose — the language name on a fence buys
// nothing without a highlighter, and allowing it would let agent output name any
// class in the app. `style` likewise, which costs table alignment and no more.
const SANITIZE = {
  ALLOWED_TAGS: [
    'p', 'br', 'hr', 'strong', 'em', 'del', 's', 'code', 'pre', 'blockquote',
    'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'a',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
  ],
  ALLOWED_ATTR: ['href', 'title', 'target', 'rel'],
  // Both halves of what the parser accepted, and nothing wider.
  ALLOWED_URI_REGEXP: new RegExp(`${SAFE_LINK.source}|${RUN_REF_RE.source}`),
  // Required, not decorative: DOMPurify checks the *value* of every allowed
  // attribute that is not declared URI-safe against ALLOWED_URI_REGEXP, so a
  // narrow one silently dropped `_blank` and `noopener noreferrer` in the browser
  // while markdown-it's output — all a Node probe sees — still had them.
  ADD_URI_SAFE_ATTR: ['target', 'rel'],
  ALLOW_DATA_ATTR: false,
  ALLOW_ARIA_ATTR: false,
}

// Memoised on the text, across instances. The computed below already covers one
// component being re-rendered with the same text — which is the once-a-second
// case, since App.vue ticks `now` to keep the relative times honest — and this
// covers the other one: a pane that is destroyed and rebuilt (a closed run
// detail, older turns paged in) would otherwise re-parse and re-sanitise every
// message it has already rendered once, on a phone.
const RENDER_CACHE_MAX = 200
const renderCache = new Map()

function renderMarkdown(text) {
  const hit = renderCache.get(text)
  if (hit !== undefined) return hit
  // DOMPurify hands its input straight back when there is no DOM to sanitise
  // against. There always is one here — this only ever runs in a browser — so the
  // branch is not about a case that happens; it is about the belt never quietly
  // becoming a decoration if that stops being true.
  const html = DOMPurify.isSupported
    ? DOMPurify.sanitize(markdown.render(text), SANITIZE)
    : `<p>${markdown.utils.escapeHtml(text)}</p>`
  if (renderCache.size >= RENDER_CACHE_MAX) renderCache.clear()
  renderCache.set(text, html)
  return html
}

// The compact shape (T46), for a surface that is a line rather than a bubble.
// Nothing here relaxes anything the block path decided — same parser instance, so
// the same link policy, the same disabled images, the same `html: false` — and the
// tag list is narrowed rather than widened, to what an inline parse can emit. A
// block tag reaching this sanitiser would mean the parse had changed underneath
// it, and a card row is the wrong place to discover that.
const SANITIZE_INLINE = {
  ...SANITIZE,
  ALLOWED_TAGS: ['br', 'strong', 'em', 'del', 's', 'code', 'a'],
}

// Its own cache, and not a mode-keyed key into the one above: the same string
// renders to different markup in the two shapes. The four lines of bookkeeping are
// repeated rather than factored out with it, so that the one line worth reading in
// either function — which parse, and which allowlist it is checked against — is
// the line that is actually there.
const inlineCache = new Map()

function renderMarkdownInline(text) {
  const hit = inlineCache.get(text)
  if (hit !== undefined) return hit
  // No <p> around the fallback, unlike the block path: this output goes inside a
  // sentence, and a paragraph would break the line it was put in.
  const html = DOMPurify.isSupported
    ? DOMPurify.sanitize(markdown.renderInline(text), SANITIZE_INLINE)
    : markdown.utils.escapeHtml(text)
  if (inlineCache.size >= RENDER_CACHE_MAX) inlineCache.clear()
  inlineCache.set(text, html)
  return html
}
// --- end of render path ------------------------------------------------------

const props = defineProps({
  // What the agent wrote, as it was written. Untrusted by definition: it quotes
  // the web, it pastes tool output, and on a bad day it is repeating something a
  // repository told it to repeat.
  text: { type: String, default: '' },
  // Render it so that it cannot produce a block element — for the surfaces that
  // are one line of a card or a row. See the render path above for what that
  // costs (a heading stays a `#`) and what it buys (a row stays a row).
  inline: { type: Boolean, default: false },
})

const rendered = computed(() => (props.inline
  ? renderMarkdownInline(props.text)
  : renderMarkdown(props.text)))

// Delegated, because the markup is injected: one listener per rendered block,
// which also covers text that arrives later. `preventDefault` runs for any run
// reference, dispatched or not — a click that reached the browser would hand the
// href to whatever the OS registered for the scheme.
//
// Bound for `auxclick` too (`click` is the primary button only), which is why the
// handler filters on `button`: `auxclick` fires for every non-primary button, and
// a right-click must keep doing nothing but open its own menu. A context-menu
// "open in new tab" is not cancellable, so the claim is about clicks, not about
// the href never leaving the page.
function onClick(event) {
  // `auxclick` is every non-primary button, not just the middle one — so
  // without this a right-click switched the view *and* opened the context menu
  // (`preventDefault` on auxclick does not suppress `contextmenu`, which is a
  // separate event). Before this handler bound auxclick at all, a right-click on
  // a reference did nothing, so the filter restores that rather than inventing a
  // rule: button 0 arrives as `click`, button 1 is the middle click this is for.
  if (event.type === 'auxclick' && event.button !== 1) return
  const anchor = event.target?.closest?.('a')
  if (!anchor) return
  const ref = parseRunRef(anchor.getAttribute('href') || '')
  if (!ref) return
  event.preventDefault()
  dispatchRunRef(ref)
}
</script>

<template>
  <!-- The one v-html in the app. What it is bound to has been through both
       layers: the parser cannot emit HTML, and the result is allowlisted before
       it gets here. A <div>, not a <p>, because markdown produces block elements
       and a browser would tear a paragraph apart around them — and a <span> in
       inline mode, because that output goes *inside* a line somebody else wrote
       and a block root would break it in two. One element either way, so there is
       still exactly one binding to keep an eye on. -->
  <component
    :is="inline ? 'span' : 'div'"
    class="markdown"
    v-html="rendered"
    @click="onClick"
    @auxclick="onClick"
  />
</template>

<style scoped>
/* Rendered markdown is injected by v-html, so none of it carries this
   component's scope attribute: without :deep() every rule below matches nothing
   at all, silently, and the fence rule is not one to lose silently.

   The root is styled without it, because that element *is* compiled from the
   template above. Both declarations are here rather than in each consumer, since
   a caller that forgot either one would scroll the whole page sideways on a
   phone: `anywhere` because a long path or URL is routine in this text, and
   `min-width: 0` because a flex or grid child otherwise refuses to shrink below
   its content and takes the page width with it. */
.markdown {
  overflow-wrap: anywhere;
  min-width: 0;
}

/* A rendered block sits inside a bubble or a card that already spaced it, so it
   brings no margin of its own at either end — only between its parts. */
.markdown :deep(> :first-child) {
  margin-top: 0;
}

.markdown :deep(> :last-child) {
  margin-bottom: 0;
}

.markdown :deep(p) {
  margin: 0 0 8px;
}

.markdown :deep(ul),
.markdown :deep(ol) {
  margin: 0 0 8px;
  padding-inline-start: 22px;
}

/* An agent's headings are section markers inside one bubble, not page titles;
   left at browser defaults an `#` heading is twice the height of the turn. */
.markdown :deep(h1),
.markdown :deep(h2),
.markdown :deep(h3),
.markdown :deep(h4),
.markdown :deep(h5),
.markdown :deep(h6) {
  font-size: 1rem;
  font-weight: 600;
  margin: 10px 0 6px;
}

.markdown :deep(blockquote) {
  margin: 0 0 8px;
  padding-inline-start: 10px;
  border-inline-start: 3px solid rgba(var(--v-border-color), 0.3);
}

/* Wide tables scroll in their own box rather than widening the bubble, which on
   a phone would widen the page. `display: block` is what makes overflow apply. */
.markdown :deep(table) {
  display: block;
  overflow-x: auto;
  border-collapse: collapse;
  margin: 0 0 8px;
}

.markdown :deep(th),
.markdown :deep(td) {
  border: 1px solid rgba(var(--v-border-color), 0.3);
  padding: 3px 8px;
  text-align: start;
}

/* Inline code wraps: style.css sets `nowrap` on every <code>, and a long path in
   backticks would push the bubble past the edge of a phone. */
.markdown :deep(code) {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* A fence is something the operator pastes into a shell, so it scrolls sideways
   in its own box and never reflows: a wrapped command reads as two lines, and
   the half of it that gets pasted is a real hazard. The `white-space` has to be
   restated on the <code> inside — style.css's `nowrap` would otherwise collapse
   the newlines out of the block and leave the whole script on one line. */
.markdown :deep(pre) {
  margin: 0 0 8px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgb(var(--v-theme-code));
  color: rgb(var(--v-theme-on-code));
  white-space: pre;
  overflow-wrap: normal;
  overflow-x: auto;
}

.markdown :deep(pre code) {
  white-space: pre;
  overflow-wrap: normal;
  background: none;
  color: inherit;
  padding: 0;
  border-radius: 0;
}
</style>
