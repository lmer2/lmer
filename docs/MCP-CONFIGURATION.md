# MCP Configuration Guide

This document explains how Model Context Protocol (MCP) servers are configured in the LMER container environment.

## Overview

MCP servers extend Claude's capabilities by providing additional tools. The container comes pre-configured with Playwright MCP for browser automation.

## Configuration Files

### Project MCP Configuration (`.mcp.json`)

The main MCP server configuration is in `.mcp.json` at the project root. This file is copied into the container at build time.

```json
{
  "mcpServers": {
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"]
    }
  }
}
```

### Personal MCP Configuration (`.mcp.local.json`)

To add personal MCP servers without modifying the shared configuration:

1. Create `.mcp.local.json` in your `~/.lmer/` directory
2. The runner script will merge this with the base `.mcp.json` at container startup

Example `~/.lmer/.mcp.local.json`:
```json
{
  "mcpServers": {
    "memory": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-server-memory"]
    },
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-server-filesystem", "/workspace"]
    }
  }
}
```

### Permissions (`.claude/settings.json`)

MCP tool permissions are configured in `.claude/settings.json`. The Playwright MCP tools are pre-approved:

```json
{
  "permissions": {
    "allow": [
      "mcp__playwright__*"
    ]
  }
}
```

To add permissions for additional MCPs, add entries to `.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__memory__*",
      "mcp__filesystem__*"
    ]
  }
}
```

## Adding Third-Party MCPs

### Step 1: Add Server Configuration

Add the MCP server to your `.mcp.local.json`:

```json
{
  "mcpServers": {
    "your-mcp-name": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@your-org/mcp-server-name"]
    }
  }
}
```

### Step 2: Add Permissions

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

### Step 3: Rebuild or Restart Container

For runtime changes, restart the container. For permanent changes to the base image, update the main config files and rebuild.

## Pre-installed MCPs

### Playwright MCP

Playwright MCP enables browser automation and web scraping. Pre-installed tools include:

| Tool | Description |
|------|-------------|
| `mcp__playwright__browser_navigate` | Navigate to a URL |
| `mcp__playwright__browser_snapshot` | Get accessibility snapshot (preferred for actions) |
| `mcp__playwright__browser_take_screenshot` | Take screenshot of page |
| `mcp__playwright__browser_click` | Click an element |
| `mcp__playwright__browser_type` | Type text into element |
| `mcp__playwright__browser_fill_form` | Fill multiple form fields |
| `mcp__playwright__browser_evaluate` | Execute JavaScript |
| `mcp__playwright__browser_close` | Close the browser |

Example usage in Claude:
```
Navigate to https://example.com and take a screenshot
```

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
2. Verify npx can access the package: `npx -y @package/name --help`
3. Check container logs for merge errors

### Permission Denied for MCP Tool

1. Verify tool permission in settings: `mcp__servername__*`
2. Check that permissions are merged correctly at startup
3. Look for "Merged personal permissions" message in startup output

### Browser Not Working (Playwright)

1. Ensure Chromium is installed: `playwright install chromium`
2. Check system dependencies (the container includes these)
3. For headless issues, verify DISPLAY is not set in container

## File Locations Summary

| File | Location | Purpose |
|------|----------|---------|
| `.mcp.json` | Project root | Shared MCP server config |
| `.mcp.local.json` | `~/.lmer/` or `/home/developer/` | Personal MCP servers |
| `.claude/settings.json` | Project root | Shared permissions |
| `.claude/settings.local.json` | `~/.lmer/.claude/` | Personal permissions |
