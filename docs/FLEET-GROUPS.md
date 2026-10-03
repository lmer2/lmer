# Graph groups

Open **Platform settings → Graph groups** to give related repository clusters a
shared label. Add a group name and matching text, one entry per line. Use **Up**
and **Down** to set priority, then **Save groups**. Changes apply when returning
to the graph; other open graphs receive them on their next normal state poll.

Matching text is **case-insensitive text contained in the repository project
path**, excluding the host and run name. Any entry in a group may match. The
first matching group wins. All characters are literal: `*`, brackets and
parentheses have no special meaning. Leading and trailing whitespace is trimmed;
the editor ignores blank lines.

For example, a group named **Team** with two entries, `team/` and `tools/team-`,
matches `team/widget`, `TEAM/service` and `tools/team-site`, but not
`teamwork/service` or `tools/other-site`. Because this is contained text, `team/`
also matches `notteam/widget`; use a more specific entry when needed. A later
**Widget** group matching `widget` will not take `team/widget` from **Team**.

Groups are optional. Fresh, missing or empty configuration keeps ordinary
repository clusters. Remove every group and save to clear the configuration.
A group containing only one repository needs no extra ring. Visual grouping
does not assign runs to named projects or change ownership, access or the
selected project filter.

Configuration belongs to the platform instance and survives browser reloads and
daemon restarts. There are no built-in group names and no migration that seeds
old defaults. Names must be unique ignoring case, contain 1–64 characters, and
have 1–16 nonempty matching entries of at most 256 characters each. At most 32
groups are accepted; control characters are refused. Save errors identify the
group or entry, retain the draft and leave stored settings unchanged. Invalid
manually edited groups are reported in settings and render as no extra groups,
so the editor remains available to repair them.

## Configuration and API

The platform's existing `config.json` stores an ordered list:

```json
{
  "fleet_groups": [
    {"name": "Team", "patterns": ["team/", "tools/team-"]},
    {"name": "Widget", "patterns": ["widget"]}
  ]
}
```

`GET /api/settings` returns `fleet_groups` and `fleet_groups_problem` (normally
`null`). An authenticated operator can submit `POST /api/settings` with
`{"fleet_groups": [...]}` or `{"fleet_groups": []}` to clear. The existing
settings route applies a validated atomic patch, preserving unrelated settings
and unknown stored keys. Invalid input returns HTTP 400 before any part of the
patch is written. `null` is invalid; use an empty list to clear. There is no
environment override and these are not project or assistant settings.

`GET /api/state` carries fresh values under `config.fleet_groups` and
`config.fleet_groups_problem`. The browser uses bounded literal string searches,
never compiles entered text into regular expressions, and relayouts changed group
membership even when the visible runs are unchanged. Identical polls keep the
settled layout.
