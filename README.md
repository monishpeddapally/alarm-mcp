# alarm-mcp

An MCP server that lets any LLM client (Claude mobile, ChatGPT mobile, Claude Desktop, Cursor, Computer, etc.) set **event-driven alarms** in plain English. The server polls live data sources and fires a real alarm — by default, a loud push to your phone via ntfy.sh — when the condition becomes true.

```
> wake me when Rishabh Pant comes to bat
> alert me if BTC drops below $50,000
> let me know when Trump starts speaking live
```

## TL;DR for phone use

Deploy to Fly.io → connect Claude/ChatGPT mobile to the URL → install ntfy on your phone. **See [DEPLOY.md](DEPLOY.md) for the 5-minute setup.**

## How it works

```
LLM client ──tool call──▶ alarm-mcp ──┐
                                       │
                                       ├─▶ source plugins (cricket, price, web)
                                       ├─▶ LLM evaluator ("is condition true now?")
                                       └─▶ alarm output (sound / Shortcut / ntfy / webhook)
```

A single background loop checks each registered alarm on its own cadence (default 30 s). When the evaluator says "yes, the condition is satisfied now," the alarm output channels fire in parallel.

## Tools exposed

| Tool            | Purpose                                                    |
| --------------- | ---------------------------------------------------------- |
| `create_alarm`  | Register a natural-language alarm condition                |
| `list_alarms`   | List every alarm + state                                   |
| `get_alarm`     | Detail for one alarm                                       |
| `check_now`     | Run one immediate check (don't wait for the next poll)     |
| `test_trigger`  | Force-fire an alarm — useful to verify your sound/Shortcut |
| `cancel_alarm`  | Delete an alarm                                            |

## Two ways to run

### A. Remote (phone-first) — Fly.io + ntfy push

See **[DEPLOY.md](DEPLOY.md)**. This is the recommended setup if your goal is "my phone rings when X happens."

### B. Local (laptop only) — stdio MCP

```bash
git clone <this folder> alarm-mcp && cd alarm-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Then point Claude Desktop / Cursor at the `alarm-mcp` binary via stdio (see "Wire it up" below).

## Configure

Set environment variables before launching:

```bash
# Required for arbitrary conditions ("Trump is speaking now"). Without it,
# the server falls back to a keyword heuristic that only handles
# simple "named person appears" style conditions.
export ANTHROPIC_API_KEY=sk-ant-...
export ALARM_MCP_MODEL=claude-3-5-haiku-latest   # optional, default shown

# --- Alarm output channels (any combination) ---

# 1. macOS local sound + banner — auto-enabled on macOS, no setup.
export ALARM_MCP_RING_SECONDS=20

# 2. Real iOS/macOS Alarm app via Apple Shortcuts.
#    Create a Shortcut named "Fire Alarm MCP" that "Creates Alarm" or plays
#    a custom sound, then set:
export ALARM_MCP_SHORTCUT="Fire Alarm MCP"

# 3. Push to phone (rings loudly). Install ntfy app, subscribe to your topic.
export ALARM_MCP_NTFY_TOPIC=monish-cricket-alerts-xyz123

# 4. Generic webhook (Slack, Discord, Zapier, your own URL).
export ALARM_MCP_WEBHOOK="https://hooks.slack.com/services/..."
```

## Wire it up

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "alarms": {
      "command": "/absolute/path/to/alarm-mcp/.venv/bin/alarm-mcp",
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "ALARM_MCP_SHORTCUT": "Fire Alarm MCP",
        "ALARM_MCP_NTFY_TOPIC": "monish-cricket-alerts-xyz123"
      }
    }
  }
}
```

Restart Claude Desktop. You should see the alarm tools in the tools panel.

### Cursor / Continue / any MCP client

Point it at the `alarm-mcp` executable with stdio transport. Same env vars apply.

## Bridging to the real iOS/macOS Alarm app

Apple does not expose a public API for the Clock/Alarm app. The official bridge is **Apple Shortcuts**:

1. Open the Shortcuts app.
2. Create a new Shortcut named exactly `Fire Alarm MCP`.
3. Add actions, e.g.:
   - **Get Dictionary from Input** (the MCP sends JSON in)
   - **Get Value for `label`** from the dictionary
   - **Create Alarm** → time = "now + 30 seconds", label = the variable above
   - (or) **Show Notification** + **Play Sound**
4. Save. Now the MCP can trigger it any time via the `shortcuts run` CLI, which is automatic when `ALARM_MCP_SHORTCUT` is set.

If you want push-to-phone you can also use **Pushcut** instead of Shortcuts — it has a "trigger from URL" feature and a richer alarm-style UI.

## Example session

```
You: wake me when Rishabh Pant comes to bat in the next India match
Claude (uses create_alarm):
     condition="Rishabh Pant is currently batting (striker or non-striker) in any live India match"
     source_hint="cricket"
     poll_seconds=20

→ alarm 224398d4 created, status=pending
... (server polls every 20 s, transitions to "armed" after first check) ...
... (when snapshot shows "striker: RR Pant 12(8)") ...
→ alarm fires: sound + macOS banner + Shortcut runs "Create Alarm now"
```

## State

Alarms persist in `~/.alarm-mcp/state.json` (override with `ALARM_MCP_STATE_DIR`). Triggered/cancelled alarms remain visible via `list_alarms` until you cancel them.

## Limits / known issues

- **Data freshness**: cricket snapshots update as fast as cricbuzz's site does (a few seconds behind broadcast). News snapshots may lag minutes. Don't use this for second-critical events.
- **LLM evaluator cost**: each poll = 1 Claude Haiku call (~200 tokens in, ~80 out). At 30 s polling that's ~120 calls/hour. Use longer `poll_seconds` for non-time-critical alarms.
- **Cricbuzz**: if both the direct site and the `cricbuzz-live` mirror are unreachable, cricket alarms can't evaluate. The server logs the error and keeps trying.
- **No public Alarm app API**: the cleanest route is the Shortcuts bridge above.

## Extending

Sources are plugins — see `alarm_mcp/sources.py`. To add e.g. a sports API for football, write a function returning a text snapshot and add a routing case in `fetch_snapshot`. Everything downstream is source-agnostic.
