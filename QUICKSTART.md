# Quickstart — 5 minutes

Full documentation is in [README.md](README.md). This is just the fastest path to a
running bot.

---

## Step 1 — Open the folder in VS Code

Unzip it somewhere, then:

```bash
code ca-sentinel
```

## Step 2 — Install

**macOS / Linux**

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Then in VS Code: `Ctrl/Cmd+Shift+P` → **Python: Select Interpreter** → pick `./.venv`.

## Step 3 — Get your two Telegram credentials

### `TELEGRAM_BOT_TOKEN`

1. Open Telegram, message **[@BotFather](https://t.me/BotFather)**
2. Send `/newbot`
3. Give it a display name, then a username ending in `bot`
4. BotFather replies with the token — looks like `8123456789:AAE1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P`

### `TELEGRAM_CHAT_ID`

Pick where alerts should land:

| Destination | Setup first |
|---|---|
| **Your own DM** | Open your new bot and send it any message (e.g. `hi`) |
| **A group** | Add the bot to the group, then send any message in it |
| **A channel** | Add the bot as an **Administrator** with **Post Messages** enabled, then post anything |

Then run this, pasting in your token:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
```

Look for `"chat": { "id": ... }` and copy that number.

- DM → positive, e.g. `812345678`
- Group / channel → negative, e.g. `-1001234567890`

> **Empty `result: []`?** You haven't sent a message to the bot yet, or the bot isn't in
> the channel. Send one and re-run. For channels, look for `channel_post` instead of
> `message`.

## Step 4 — Fill in `.env`

```bash
cp .env.example .env
```

Open `.env` and set:

```ini
TELEGRAM_BOT_TOKEN=8123456789:AAE1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P
TELEGRAM_CHAT_ID=-1001234567890
CONTACT_EMAIL=you@example.com
```

`CONTACT_EMAIL` is advertised in the User-Agent so a site owner can reach you.

## Step 5 — Verify, then run

```bash
./.venv/bin/python run.py test-telegram
```

You should get a ✅ message in Telegram. If so:

```bash
./.venv/bin/python run.py run
```

Leave it running. Stop with `Ctrl+C`.

---

## From inside VS Code

Press **F5** and pick a configuration:

| | |
|---|---|
| **1. Test Telegram** | Run this first — confirms token + chat id |
| **2. Check a URL** | Dry run: fetches one page, prints what would alert. Sends nothing |
| **3. Run the monitor** | The real thing |
| **Stats** / **Recent addresses** | Inspect what it has found |

Tests appear in the **Testing** sidebar (flask icon), or:

```bash
./.venv/bin/python -m pytest
```

---

## What to expect

**First run** records the 3 addresses currently published on suppoman.com **silently** —
those aren't new, so alerting on them would be noise. You'll see:

```
CHANGE detected https://suppoman.com/secretarea/ (status=200, 4475 bytes, 556ms) [first pass]
seeded existing evm 0x008Df4b3E857D06c4603Aeb11F267ccD32ce2005
seeded existing evm 0x5B6Ef408c4eBb166788C0cA4cB644f12AC757777
seeded existing solana 8XkvLLFHBJvkmCQkHfK4pEymsiZkCXdouSCRpZw5Sbj8
```

**After that it goes quiet.** That is correct — it's polling every 2 seconds and getting
`304 Not Modified`. A heartbeat line every 5 minutes confirms it's alive:

```
heartbeat uptime=300s targets=2 checks=180 changes=0 addresses=3 alerted=0 pending=0
```

**When a new CA appears**, within ~2.5 seconds you get a Telegram message with the
address, chain, page, confidence score and explorer links.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set` | `.env` missing or in the wrong folder — it goes next to `config.yaml` |
| `401 Unauthorized` | Token is wrong or has a typo/extra space |
| `400 Bad Request: chat not found` | Wrong chat id, or you never messaged the bot |
| `403 Forbidden: bot is not a member` | For channels, add the bot as an **admin** with Post Messages |
| Nothing happens after startup | Correct — it's idle until the page changes. Check the heartbeat |
| Want to re-test alerting | Delete `data/sentinel.db`, set `BOOTSTRAP=alert` in `.env`, restart |

Logs: `logs/sentinel.log` (rotating) and `logs/hits.log` (every address ever found).
State: `data/sentinel.db` — **keep this**, it's the dedup memory.
