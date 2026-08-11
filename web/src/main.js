import { createApp, watch } from 'vue'
import { createVuetify } from 'vuetify'
import 'vuetify/styles'
// SVG icon set: the path data for the handful of icons this UI uses is compiled
// into the bundle. Vuetify's default set is the MDI webfont, which would mean a
// font file of a few hundred KB fetched at first paint — and on a LAN with no
// route out, an icon font that fails to load is a UI of empty boxes.
import { aliases, mdi } from 'vuetify/iconsets/mdi-svg'
import App from './App.vue'
import './style.css'

// The tone palette is the fleet view's signalling system, so it lives in the
// theme rather than in components: one place decides what "needs you" looks like,
// in either scheme. The custom names (tone-*) are the ones format.js maps tones
// onto.
//
// The house colours are 20c's: grey surfaces, deep orange accent. Deep orange is
// the one constraint that shapes everything else here, because it lands squarely
// between the two colours this UI already signals with — amber "attention" and
// red "bad". Left alone, the brand accent on every button would read as urgent,
// and the one state that IS urgent would stop standing out. So the signal ramp is
// re-separated around it rather than left where a stock palette would put it:
//
//   rose (bad) · yellow (attention) · orange (brand) · green (live) · purple
//   (done) · grey (idle)
//
// Two deliberate consequences:
//   - "attention" moved off amber onto a true yellow, so it cannot be mistaken
//     for the accent at a glance on a phone in daylight;
//   - "bad" moved off orange-red onto rose, for the same reason in the other
//     direction.
// Colour is still carrying this alone today, which is the weak point: a
// red/green-blind operator gets "bad" and "live" as the same muddy tone. Pairing
// every tone with an icon is the fix, and it belongs in the components that draw
// the chips rather than here.
// The greys are neutral-to-warm on purpose. A slate grey (the usual dark-UI
// default) is a blue, and next to an orange accent the whole app reads cold and
// generic — which is exactly how the first attempt at this landed.
//
// The row that is not a run, and why this palette holds no key for it
// -------------------------------------------------------------------
// operator, live testing: "uber lmer entry in runs list (both side bar and main
// list) needs to be a different color entirely to set it apart from other runs".
// The first answer was a colour off this ramp: a cyan ink for the badge plus
// `orchestrator-ground`, a wash under the whole row — cyan because the ramp spans
// rose, yellow, orange, green and purple, so the one hue that could not be read as a
// state was the gap between green and purple.
//
// The operator threw the wash out on the next pass (2026-07-29): "i don't think
// solid background for the uber lmer row is the way -- lets instead do the
// following - give it an orange border in the main list view and in the side list
// view instead of the play icon show the robot icon". A ground repaints the row to
// say one thing about it, and with the badge on top of it there were two accent
// systems competing in one row.
//
// So the marking is now a border in `primary` on the fleet card and the filled robot
// in place of the drawer row's state icon — and neither needs a key here: the accent
// is already defined in both schemes, and a shape is not a colour at all. Both cyan
// keys are deleted rather than left unused, because a colour nothing paints with is
// the next "nicer" tint waiting to be found. The components carry the rest of the
// reasoning (RunCard.vue's scoped rule, RunNav.vue's prepend slot).
//
// The terminal's own surface (terminal / on-terminal)
// --------------------------------------------------
// operator, live testing: "i think the terminal background could be darker
// (grey-darken-4 maybe)". It was the card's `surface`, which is white in the light
// scheme — a ground the ANSI colours a harness paints with were never chosen for.
// So the emulator gets a surface of its own, dark in *both* schemes: the same bytes
// have to stay readable on a phone in daylight and on a dark desk, and a terminal is
// a dark box everywhere else an operator meets one.
//
// Deliberately not grey-darken-4 itself (#212121): it is *lighter* than this
// scheme's own background, so taking the suggestion literally would have made the
// terminal a pale patch on a dark page. The value is that idea in the house's warm
// greys, one step below the darkest surface the app already paints.
//
// The conversation's three grounds (chat-*)
// -----------------------------------------
// operator, live testing: "assistant messages, user messages, assistant actions
// should all be color coded backgrounds". Three classes of turn, three grounds,
// and they are here rather than in Chat.vue for the reason the tone ramp is: a
// colour written into a component is one the scheme switcher cannot repaint, and
// half of these are read on a dark desk and the other half on a bright phone.
//
// They are one ramp, and the order along it is the same in both schemes —
// operator nearest the reader, agent between, machinery furthest back:
//
//   chat-operator  the surface carrying a wash of `primary` (14% dark, 20%
//                  light). What *you* sent is the one thing in the pane worth the
//                  brand accent, and hue is what separates it from the agent's
//                  ground: a thumb-scroll reads warm-vs-neutral before it reads
//                  which side a bubble leans to.
//   chat-agent     a neutral step of the same warm grey, away from the card's
//                  surface — down in both schemes, because a light scheme's
//                  surface is already white and a dark one's step up is `code`,
//                  which has to stay visible as a chip on top of this.
//   chat-action    one step further back, so tool rows and the internals behind
//                  the "show N internal" toggle read as machinery under the
//                  conversation rather than as a third voice in it.
//
// Chosen against the ink that actually lands on them, which is the medium-emphasis
// header rather than body text: every ground clears WCAG AA (4.5:1) for that
// dimmest case — 5.15:1 worst in light, 7.44:1 worst in dark — and full-strength
// prose clears 12:1 everywhere. A fourth class fits the same prefix; nothing here
// assumes there are three.
const light = {
  dark: false,
  colors: {
    background: '#f5f4f2',
    surface: '#ffffff',
    primary: '#c2410c',
    success: '#1f883d',
    warning: '#926a00',
    error: '#be123c',
    'tone-idle': '#5f5b57',
    'tone-done': '#6639ba',
    // The emulator's surface, dark in this scheme too, with an ink of its own
    // because `on-surface` here is near-black.
    terminal: '#141312',
    'on-terminal': '#e8e6e3',
    'chat-agent': '#eae7e2',
    'chat-operator': '#f3d9ce',
    'chat-action': '#dedbd5',
    // style.css renders <code> against these; without them the declarations are
    // invalid and inline code loses its background in both schemes.
    // A step *deeper* than every ground above, which is the only direction open
    // to it: most of this UI's inline code is a target, a path or a session id
    // drawn on a card, and in a light scheme that card is white — a paler chip
    // has nowhere left to go and reads as no chip at all. Warm for the reason the
    // greys are warm; a cool near-white here was the one patch of a different
    // palette in the pane, and it landed on the conversation's own grounds.
    code: '#d0c9bd',
    'on-code': '#16191d',
  },
}

const dark = {
  dark: true,
  colors: {
    background: '#1b1a19',
    surface: '#252423',
    // Deep, not bright. A lighter orange (#ff7043 and friends) turns every
    // filled button into a salmon slab that shouts louder than the accent is
    // meant to; the darker tone reads as the brand and lets white sit on it.
    primary: '#e64a19',
    success: '#3fb950',
    warning: '#ffd23f',
    error: '#f43f5e',
    'tone-idle': '#9a9793',
    'tone-done': '#a371f7',
    terminal: '#141312',
    'on-terminal': '#e8e6e3',
    'chat-agent': '#1b1a19',
    'chat-operator': '#402922',
    'chat-action': '#0f0e0e',
    code: '#2e2c2a',
    'on-code': '#e8e6e3',
  },
}

const vuetify = createVuetify({
  theme: {
    // 'system' is Vuetify's own prefers-color-scheme binding (it reads the media
    // query and re-reads it on change), which is what the hand-rolled stylesheet
    // got from an @media block. Stated explicitly because "the OS decides" is a
    // requirement here, not a default worth inheriting silently.
    //
    // Still the default, and still what an operator who has never picked gets:
    // App.vue's switcher overrides it per operator (theme.change) and remembers
    // 'system' as one of its three states, so this is the value that survives an
    // empty browser store rather than a value that has been superseded.
    defaultTheme: 'system',
    themes: { light, dark },
  },
  icons: { defaultSet: 'mdi', aliases, sets: { mdi } },
  // House look, set once. An operator decision: no outlined and no flat — those
  // read as unfinished wireframe rather than a product, so surfaces get real
  // elevation and fields get a filled ground. Chips run one size down from Vuetify's
  // default, which is built for a chip as a form control rather than as the
  // dense status marker this UI uses it for.
  //
  // Note these are DEFAULTS: a `variant` prop written on a component beats them
  // outright, so any component still hardcoding one has to be swept by hand.
  defaults: {
    VCard: { variant: 'elevated', elevation: 2 },
    VBtn: { variant: 'elevated' },
    VChip: { size: 'small', label: true },
    VAlert: { variant: 'tonal', density: 'comfortable' },
    VTextField: { variant: 'filled', density: 'comfortable' },
    VTextarea: { variant: 'filled', density: 'comfortable' },
    VSelect: { variant: 'filled', density: 'comfortable' },
    VCombobox: { variant: 'filled', density: 'comfortable' },
  },
})

// --- browser chrome (extracted by tests/test_platform_web_theme.py) ----------
// The strip of the phone's own UI around the page. index.html decides it for the
// first frame, from the two theme-color metas, because none of this exists yet at
// that point — and its inline script covers the operator who forced a scheme,
// since those metas answer prefers-color-scheme, which is the OS's answer rather
// than theirs.
//
// From the mount on, the theme is the authority: `current` is the theme actually
// being painted, with 'system' already resolved against the OS and re-resolved
// when the OS flips at sunset, so the chrome follows a scheme picked in the app
// bar without a reload and follows a mid-session sunset without a stale strip.
// Reading the colour off the theme rather than restating it is also what keeps
// this from being a third place the palette lives.
//
// Both metas, and the content rather than the media query: whichever one the
// browser would have chosen now says the same thing, which is the point.
//
// Deliberately not `immediate`. This module runs before App.vue's setup, so at
// that moment the theme is still the default one and painting from it would put
// the OS's colour back over what index.html just decided — the flash again, one
// line further out. Nothing needs painting until the theme changes, and applying
// a stored preference is a change; a stored 'system' resolves to the theme that
// is already current, so it correctly leaves the media queries in charge.
watch(() => vuetify.theme.current.value.colors.background, (background) => {
  for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
    meta.content = background
  }
})
// --- end of browser chrome ---------------------------------------------------

createApp(App).use(vuetify).mount('#app')
