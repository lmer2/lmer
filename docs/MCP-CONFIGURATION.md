# MCP Configuration Guide

This document explains how Model Context Protocol (MCP) servers are configured in the LMER container environment.

## Overview

MCP servers extend Claude's capabilities by providing additional tools. **lmer ships no MCP server of its own** — `.mcp.json` is present but empty, and this document is how you add one.

That is a deliberate default. Every wired server puts its full tool schema into the system prompt of every session that loads it, whether or not the session ever calls it, so a server nobody uses is a standing tax. Wire what you need; leave the rest out.

> **Browser automation is not an MCP here.** It used to be: the container pre-wired `@playwright/mcp` and pre-approved `mcp__playwright__*`. That is gone (issue #410). Driving a browser goes through the **`chrome-agent` skill** — a CLI over the Chrome DevTools Protocol, costing one `Bash(...)` grant instead of ~5K tokens of tool schema. See `agent-files/claude/skills/chrome-agent/SKILL.md`. Playwright itself stays in the image: it manages the Chromium that skill drives, and it is still how browser tests are authored.

## Configuration Files

### Project MCP Configuration (`.mcp.json`)

The base MCP server configuration is `.mcp.json` at the project root. It is copied into the container at build time, to `/home/developer/.mcp.json`.

As shipped it wires nothing:

```json
{
  "mcpServers": {}
}
```

The file stays present and valid because it is the base of the merge below — a personal `.mcp.local.json` is merged *into* it at container start, which needs an `mcpServers` object to merge into.

**Where a server actually loads from.** Claude Code reads *project* MCP config from `<project>/.mcp.json`, and a session's project is its working directory, `/workspace` (Containerfile `WORKDIR`). So the file that loads is `/workspace/.mcp.json` — the target repo's own. This repo's `.mcp.json` was ever loaded only in lmer self-development, where `/workspace` *is* the lmer checkout; that is why removing the Playwright server changed nothing for target-repo work.

The copy at `/home/developer/.mcp.json` is a different file and is **not** project scope for a `/workspace` session. Which means the `.mcp.local.json` merge below, which rewrites that copy, does not reach an ordinary session either — worth knowing before you spend time on it. To wire a server for a target repo, put it in that repo's `.mcp.json`.

### Personal MCP Configuration (`.mcp.local.json`)

To add personal MCP servers without modifying the shared configuration:

1. Get a `.mcp.local.json` to `/home/developer/.mcp.local.json` **inside the container** (or wherever `LMER_MCP_LOCAL_FILE` points)
2. The runner script merges it into `/home/developer/.mcp.json` at container startup (`LMER_MCP_FILE` points it elsewhere)

Read the scope note above first: the merged file is not project scope for a `/workspace` session, so unless `LMER_MCP_FILE` is pointed at the project's own `.mcp.json`, this merge produces a file nothing reads.

**Delivering the file is on you:** no lmer mount copies a host `~/.lmer/.mcp.local.json` into the container today, so the merge only sees a file something else placed at that in-container path — e.g. an explicit `--mount-file ~/.lmer/.mcp.local.json:/home/developer/.mcp.local.json` bind (`LMER_MOUNT_FILES` for the persistent form).

Example `/home/developer/.mcp.local.json`:
```json
{
  "mcpServers": {
    "memory": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory"]
    }
  }
}
```

### Permissions (`.claude/settings.json`)

MCP tool permissions are configured in `.claude/settings.json`. lmer pre-approves no `mcp__*` pattern, because it wires no server — a grant for a server nobody runs is dead configuration, and `tests/test_browser_automation_wiring.py` asserts none creeps back in.

Add permissions for your own servers in `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__memory__*"
    ]
  }
}
```

## Adding an MCP Server

### Step 1: Pin the version

Whatever you wire, name an exact version rather than `@latest` or a floating tag. A floating spec refetches on every server start, which is startup latency and a supply-chain surface on each launch, and it means the version you tested is not necessarily the version that runs.

### Step 2: Add Server Configuration

Add the MCP server to your `.mcp.local.json`:

```json
{
  "mcpServers": {
    "your-mcp-name": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@your-org/mcp-server-name@1.2.3"]
    }
  }
}
```

If the package is baked into the image, point `command` at the installed entry point rather than at `npx`, so nothing is fetched at run time.

### Step 3: Add Permissions

Add tool permissions to `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__your-mcp-name__*"
    ]
  }
}
```

### Step 4: Rebuild or Restart Container

For runtime changes, restart the container. For permanent changes to the base image, update the main config files and rebuild.

## When a CLI beats an MCP

Before wiring a server, ask whether a command-line tool would do. lmer's browser automation moved this way (#410) and the trade generalises:

| | MCP server | CLI + a skill |
|---|---|---|
| Context cost | full tool schema in every session that loads the server | one `Bash(tool:*)` grant, plus whatever the skill says |
| Repeatability | the model is in the loop on every step | a sequence can be scripted once and replayed with no model |
| Discovery | tools are listed for you | the skill has to teach the verbs |

An MCP earns its place when the capability genuinely needs structured tool calls, or when there is no CLI. Otherwise a CLI plus a skill is cheaper on every axis except discovery.

## How Settings Merge Works

At container startup, the runner script merges configuration files:

1. **MCP Servers**: `.mcp.json` + `.mcp.local.json` → merged `.mcp.json`
   - Local servers are added to base servers
   - If same server name exists, local overrides base

2. **Permissions**: `settings.json` + `settings.local.json` → merged `settings.json`
   - Allow lists are combined (union)
   - Deny lists are combined (union)
   - Local settings override other fields

## Troubleshooting

### MCP Server Not Available

1. Check that the server is in `.mcp.json` or `.mcp.local.json`
2. Confirm the session's project scope: a server in `/home/developer/.mcp.json` does not load for a session working in `/workspace` (see the scope note above)
3. Verify the command runs standalone: `npx -y @package/name@version --help`
4. Check container logs for merge errors — look for `Merged personal MCP servers into .mcp.json`

### Permission Denied for MCP Tool

1. Verify tool permission in settings: `mcp__servername__*`
2. Check that permissions are merged correctly at startup
3. Look for "Merged personal permissions" message in startup output

### Browser Automation

Browser work is the `chrome-agent` skill, not an MCP — start at `agent-files/claude/skills/chrome-agent/SKILL.md`, which covers launching, the sense/act loop, the trusted-input escalation, the memory budget and teardown. `chrome-agent guide` prints the tool's own manual.

## File Locations Summary

| File | Location | Purpose |
|------|----------|---------|
| `.mcp.json` | Project root | Shared MCP server config (empty as shipped) |
| `.mcp.local.json` | `/home/developer/` (in-container; no mount delivers a host copy) | Personal MCP servers |
| `.claude/settings.json` | Project root | Shared permissions |
| `.claude/settings.local.json` | `/home/developer/.claude/` (in-container; no mount delivers a host copy) | Personal permissions |
