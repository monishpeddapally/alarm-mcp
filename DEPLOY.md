# Deploy to Fly.io (free tier, always-on)

This deploys `alarm-mcp` as an internet-reachable HTTPS endpoint, so any MCP-capable mobile app (Claude mobile, ChatGPT mobile, etc.) can connect to it. When an alarm fires, a loud push lands on your phone via ntfy.

## Prereqs (one-time, ~5 minutes)

1. **Install Fly CLI** on your Mac/PC:
   ```bash
   curl -L https://fly.io/install.sh | sh
   ```
   Then `fly auth signup` (or `fly auth login`). They ask for a card to prevent abuse, but the free tier covers this app.

2. **Get an Anthropic API key**: [console.anthropic.com](https://console.anthropic.com/) → API Keys → Create. Add ~$5 of credit.

3. **Pick an ntfy topic name**. Something random and unguessable, e.g. `monish-alarms-x7k2p9q4`. Topics are public — anyone who knows the name can read it, so don't use a guessable string.

4. **Install ntfy on your phone** and subscribe to that topic:
   - iOS: [App Store](https://apps.apple.com/us/app/ntfy/id1625396347)
   - Android: [Play Store](https://play.google.com/store/apps/details?id=io.heckel.ntfy) or [F-Droid](https://f-droid.org/en/packages/io.heckel.ntfy/)

5. **Pick a strong bearer token** for your MCP. Generate one:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Save it — you'll paste it into your mobile MCP client later.

## Deploy

From the `alarm-mcp` folder:

```bash
# 1. Edit fly.toml: change `app = "alarm-mcp"` to something globally unique,
#    e.g. app = "monish-alarms"

# 2. Create the app + persistent volume
fly launch --no-deploy --copy-config --name monish-alarms --region sjc
fly volumes create alarm_data --region sjc --size 1

# 3. Set your secrets (never commit these)
fly secrets set \
  ANTHROPIC_API_KEY="sk-ant-..." \
  ALARM_MCP_TOKEN="<the token from step 5>" \
  ALARM_MCP_NTFY_TOPIC="monish-alarms-x7k2p9q4"

# 4. Ship it
fly deploy
```

When it finishes, your server is live at:

```
https://monish-alarms.fly.dev
```

Test the health check:

```bash
curl https://monish-alarms.fly.dev/health
# → ok
```

And confirm auth works:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://monish-alarms.fly.dev/mcp
# → 401 (no token)
```

## Connect from your phone

### Claude mobile app

Claude's mobile app supports remote MCP servers (called "connectors" in the UI). Steps:

1. Open Claude → **Settings → Connectors → Add custom connector**
2. **Name**: `alarms`
3. **URL**: `https://monish-alarms.fly.dev/mcp`
4. **Auth**: Bearer token → paste the `ALARM_MCP_TOKEN` from above
5. Save

You should see the 6 tools (`create_alarm`, `list_alarms`, etc.) appear.

### ChatGPT mobile app

Settings → Connectors → "Create connector" → enter the same URL + bearer token. (ChatGPT calls them "deep research connectors"; same plumbing.)

### Test it

In any of those apps, type:

> Use the alarms server. Create an alarm called "phone test" with condition "BTC drops below 999999999" so it fires immediately, then test_trigger it.

Within seconds your phone should ring with the ntfy push. Cancel the alarm when done.

## Cost

| Component             | Cost                                                 |
| --------------------- | ---------------------------------------------------- |
| Fly.io machine        | Free tier (1× shared-cpu-1x / 256 MB)                |
| Fly.io volume         | Free up to 3 GB                                      |
| Anthropic Claude Haiku | ~$0.001 per check. 30 s polling = ~$0.10/day max     |
| ntfy.sh               | Free                                                 |

So practically: pennies a day while you have active alarms. Cancel triggered alarms to stop polling.

## Updating

```bash
# Edit code, then redeploy:
fly deploy
```

State persists across deploys (alarms survive) because of the mounted `/data` volume.

## Logs & debugging

```bash
fly logs              # live tail
fly status            # machine state
fly ssh console       # shell into the running machine
```

If alarms don't fire:
1. Hit `https://<app>.fly.dev/health` — should be `ok`.
2. In your MCP client call `get_alarm` on the id — look at `error` and `last_check_summary`.
3. `fly logs` will show poll-loop errors.

## Security notes

- The bearer token is the only thing protecting your server. Treat it like a password.
- ntfy topics are public — anyone who guesses your topic name can read your alarms. Use a random 16+ char topic, or self-host ntfy with auth if it matters.
- If you suspect leak: `fly secrets set ALARM_MCP_TOKEN=<new value>` regenerates instantly.
