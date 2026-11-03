// Whether the daemon answering is older than the page it served (#650). The
// daemon builds its routes once at startup but serves the UI bundle from disk on
// every request, so an update that replaced the bundle without a restart leaves
// new controls calling routes this process does not have. A daemon older than
// this check reports no `ui` at all, which is the same news.
export function daemonOutdated(health) {
  return !!health && (!health.ui || health.ui.stale === true)
}

export const DAEMON_OUTDATED =
  'The platform daemon is older than this page: the UI was updated on disk after the running ' +
  'daemon started, so it may lack routes this page uses. Restart the daemon. Until then, ' +
  'newer controls can fail with "Not Found".'
