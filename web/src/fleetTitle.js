export function fleetTitle(state, online) {
  if (!online) return '⚠ offline — lmer'
  if (state?.attention?.length || state?.totals?.attention > 0) return '⏳ waiting — lmer'
  if (state?.totals?.live > 0) return '▶ working — lmer'
  return '· idle — lmer'
}
