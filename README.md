# CA Sentinel

Watches public web pages for newly published **contract addresses** and pushes them
to Telegram within seconds of the page changing.

Built around a site-agnostic core with pluggable per-site parsers, so adding a second
or tenth website is a config entry plus (optionally) one small file.

---

## 1. How suppoman.com actually works

Measured live on 2026-09-16 before any code was written. This determined the whole design.

| Property | Finding |
|---|---|
| Hosting | Hostinger static hosting behind their `hcdn` CDN (`platform: hostinger`, `server: hcdn`) |
| Rendering | **100% static server-side HTML.** No framework, no hydration, no SSR app |
| JSON API | **None.** `script.js` contains zero `fetch`/`XHR`/`WebSocket` calls — it is a canvas background animation plus a clipboard helper |
| Where CAs live | Literal text in the HTML markup |
| `/` | `<code id="contractText">0x5B6E…7777</code>`, plus a Solana mint in the `jup.ag?buy=` link and a `dexscreener.com/solana/<pair>` chart link |
| `/secretarea/` | `<div class="contract" id="ca">0x008D…2005</div>` inside a "Latest Gem" card with an `<h2>` title and a NEW status pill |
| Cache validators | Every file serves `Last-Modified` **and** an `ETag` (size + mtime, e.g. `W/"117c-6aa1549d-…"`) |
| Edge caching | HTML responds `x-hcdn-cache-status: DYNAMIC` — pass-through to origin, so **the CDN never serves a stale page** |
| 404 behaviour | Real hard `404`s for unknown paths (not soft 200s) — clean route probing |
| `robots.txt` / `sitemap.xml` | Both absent (404) |
| Subdomains | Only `suppoman.com` and `www.suppoman.com` appear in public CT logs |
| `/secretarea` | `301` → `/secretarea/`; polling the trailing-slash form skips a round trip |

### What this means

**A headless browser would be pure overhead here.** There is no JS-rendered content, so
Playwright would cost ~300 MB of RAM and 1–3 s per page load to produce exactly the same
HTML that `curl` returns in one round trip.

**The fastest practical method is conditional-GET polling.** Because the server sends
`ETag`/`Last-Modified`, sending them back as `If-None-Match`/`If-Modified-Since` makes an
unchanged page answer **`304` with a zero-byte body**. Verified:

```
req: total=0.928s  code=304  size=0   ← first request, includes TLS handshake
req: total=0.893s  code=304  size=0
req: total=0.852s  code=304  size=0
req: total=0.329s  code=304  size=0   ← connection reused
req: total=0.418s  code=304  size=0
```

So one poll costs a single round trip on an already-open HTTP/2 connection.

**Detection latency ≈ poll interval + one RTT.** At the shipped 2 s interval for
`/secretarea/`, worst case from publish to Telegram is roughly **2.5 seconds**.

### One quirk worth knowing

The homepage's Dexscreener link is **lowercased in the site's own markup**:
`dexscreener.com/solana/2zrmpeat65m8p3hzy4epmxvwduvupq1qlr9oq6epins3`. Base58 excludes
`0`, `O`, `I` and `l`, so that string cannot be decoded or repaired — the casing is gone.
Rather than alert on garbage or drop it silently, the extractor reports it at 55%
confidence (below the 60% alert threshold) with the context
`link:dexscreener.com/path (casing lost, unverifiable)`.

The same page has also published an **EVM** address in its CA box while its buy buttons
pointed at a **Solana** mint. The parser therefore reads every surface independently and
reports whichever chain each address actually belongs to rather than assuming one.

---

## 2. Architecture

```
                 ┌──────────────────────────────────────────┐
                 │  scheduler: one asyncio task per target  │
                 └────────────────────┬─────────────────────┘
                                      │
                  ┌───────────────────▼────────────────────┐
                  │  HttpClient — pooled HTTP/2, keep-alive │
                  │  If-None-Match / If-Modified-Since      │
                  └───────────────────┬────────────────────┘
                                      │
                       304 ───────────┤─────────── 200
                    (stop, ~0 cost)   │      (body changed)
                                      ▼
                          ┌───────────────────────┐
                          │  sha256(body) differs? │  ← hash is the authority;
                          └───────────┬───────────┘     validators are the fast path
                                      ▼
                    ┌─────────────────────────────────┐
                    │  SiteParser plugin (per site)   │
                    │  anchored selectors → links →   │
                    │  labelled text → code → body    │
                    └─────────────────┬───────────────┘
                                      ▼
                    ┌─────────────────────────────────┐
                    │  validate: EIP-55 / base58-32B  │
                    │  denylist, confidence scoring   │
                    └─────────────────┬───────────────┘
                                      ▼
                  ┌───────────────────────────────────────┐
                  │  SQLite — ONE transaction:            │
                  │    mark seen  +  enqueue outbox row   │  ← no lost or
                  └───────────────────┬───────────────────┘     duplicate alerts
                                      ▼
                       ┌────────────────────────────┐
                       │  outbox worker → Telegram  │
                       │  rate limit, 429 honour,   │
                       │  exponential retry         │
                       └────────────────────────────┘
```

### Design decisions that matter

**Change detection is two-tier.** Validators are the cheap gate; `sha256(body)` is the
authority. This matters in practice — the ETag on this host embeds the content encoding
(`…;gz` vs `…;br`), so a change in negotiated compression rotates the ETag without the
content changing. The hash check absorbs that as one wasted parse instead of a false alert.

**Dedup and delivery share a transaction.** `record_candidate()` writes the seen-marker
and the outbox row atomically, so a crash can neither drop an alert nor re-fire one on
restart. Pending alerts survive a restart and are retried with backoff.

**First pass seeds silently.** On a target's first ever fetch, everything on the page is
pre-existing, not new. With `bootstrap: seed` (the default) those are recorded without
alerting, so your first notification is a genuine signal instead of a dump of the current
page. Set `bootstrap: alert` to announce the current state too.

**Confidence scoring keeps the channel clean.**

| Score | Source |
|---|---|
| 0.98 | Inside a selector the site parser explicitly points at (`#ca`, `#contractText`) |
| 0.92 | Inside a known swap/chart URL (`jup.ag?buy=`, `dexscreener.com/solana/…`) |
| 0.85 | Within ~120 chars after a "contract address" / "CA:" label |
| 0.70 | Inside `<code>`/`<pre>`/`.contract`-ish markup |
| 0.55 | Aggregator link whose address lost its casing — unverifiable |
| 0.40 | Loose body-text match |

Anything below `min_confidence` (default 0.60) is logged but never sent.

**Validation is strict.** EVM addresses that carry an EIP-55 checksum (mixed case) must
pass it — a flipped character is rejected rather than forwarded. Solana addresses must
base58-decode to exactly 32 bytes. A denylist drops wSOL, USDC/USDT, the zero address,
burn addresses and SPL program IDs, which otherwise appear on every swap link.

**Discovery runs on three independent channels.** All of them are ordinary
unauthenticated requests to public endpoints.

1. **Link crawling** (continuous) — a new page is usually linked the moment it goes live.
   Every changed page re-feeds the crawl frontier; same-site links become targets immediately.
2. **Route probing** (every 10 min) — a paced wordlist sweep for pages that are live but
   not linked yet, seeded with the global list plus the site parser's own `route_hints`.
   Calibrates against a random path first, so soft-404 sites are handled correctly.
3. **Certificate Transparency** (hourly) — public CT logs surface a new subdomain as soon
   as its TLS certificate is issued, typically before anything links to it.

A newly discovered route is itself alertable (`alert_on_new_route`), because a new page
appearing is a signal even before it has a CA on it.

---

## 3. Tech stack

| Choice | Why |
|---|---|
| Python 3.11+ / `asyncio` | One event loop handles hundreds of targets; no thread-per-page |
| `httpx[http2,brotli]` | Async, HTTP/2 multiplexing, connection pooling, conditional requests. **Brotli matters** — this host serves `br`, and without the decoder you get raw compressed bytes and silently find nothing |
| `selectolax` | C-backed HTML parser, ~10× faster than BeautifulSoup |
| SQLite (WAL) | Zero-ops durable state. Writes only happen when a page actually changes |
| `pycryptodome` | keccak-256 for EIP-55. Degrades gracefully if absent |
| Telegram Bot API over raw HTTP | Send-only; a full bot framework would be dead weight |

No headless browser in the default path — see §7 for adding one when a site needs it.

---

## 4. Folder structure

```
ca-sentinel/
├── run.py                      CLI: run / check / test-telegram / stats / recent / parsers
├── config.yaml                 sites + targets + discovery settings
├── .env                        secrets only (gitignored)
├── requirements.txt
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── deploy/
│   └── ca-sentinel.service     hardened systemd unit
├── sentinel/
│   ├── config.py               .env + YAML → typed dataclasses
│   ├── logging_setup.py        rotating sentinel.log + append-only hits.log
│   ├── models.py               FetchResult / Candidate / Target / Alert
│   ├── http.py                 pooled HTTP/2 client, conditional GET, Backoff
│   ├── chains.py               EIP-55, base58, denylist, explorer links
│   ├── extract.py              generic extractors + confidence scoring
│   ├── store.py                SQLite: page state, dedup, routes, outbox
│   ├── notify.py               Telegram sender + outbox worker
│   ├── watcher.py              per-target poll loop + ingest pipeline
│   ├── discovery.py            route probing + CT logs
│   ├── app.py                  orchestrator, signals, heartbeat
│   └── sites/                  ◄── PLUGINS LIVE HERE
│       ├── __init__.py         auto-imports every module in this folder
│       ├── base.py             SiteParser contract + registry
│       ├── generic.py          fallback — works on most sites with no code
│       └── suppoman.py         suppoman.com specifics
├── tests/
│   ├── fixtures/               real captures of both live pages
│   ├── test_chains.py
│   ├── test_extract.py
│   ├── test_store.py
│   └── test_suppoman.py
├── data/                       SQLite (gitignored)
└── logs/                       gitignored
```

The core never imports a specific site. `sites/` is the only site-aware directory.

---

## 5. Setup

### Requirements
Python 3.11 or newer.

### Install

```bash
cd ca-sentinel
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

### Create the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Decide where alerts go:
   - **Channel:** add the bot as an **administrator** with "Post Messages".
   - **Group:** add the bot to the group.
   - **DM:** send the bot any message first, so it is allowed to reply.
3. Find the numeric chat id — post a message in the destination, then:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
```

Read `result[].message.chat.id` (or `channel_post.chat.id`). Channels look like `-1001234567890`.

### Configure

```bash
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `CONTACT_EMAIL` (the last is
advertised in the User-Agent so a site owner can reach you).

### Verify before going live

```bash
./.venv/bin/python run.py test-telegram
```

```bash
./.venv/bin/python run.py check https://suppoman.com/secretarea/ --parser suppoman
```

That second command fetches the page once, runs the parser and prints exactly what would
be alerted — no database writes, no messages sent. Current output:

```
Status         200
Round trip     711 ms

1 candidate(s):

  [ALERT]   98%  evm      0x008Df4b3E857D06c4603Aeb11F267ccD32ce2005
           via selector:#ca
           label: New Secret Pick
```

---

## 6. Running

```bash
./.venv/bin/python run.py run
```

First run seeds everything currently published without alerting, then alerts only on
genuinely new addresses.

Other commands:

```bash
./.venv/bin/python run.py stats       # counters
./.venv/bin/python run.py recent 20   # recently seen addresses, alerted vs seeded
./.venv/bin/python run.py parsers     # registered site parsers
./.venv/bin/python run.py check <url> --parser <id>
```

Tests:

```bash
./.venv/bin/python -m pytest
```

### Deploy with Docker

```bash
docker compose up -d --build
docker compose logs -f
```

`./data` is bind-mounted — **keep it**, it is the dedup state. Deleting it makes every
currently published address look new again (though `bootstrap: seed` will then re-seed
them silently rather than flooding the channel).

### Deploy with systemd

```bash
sudo useradd --system --home /opt/ca-sentinel sentinel
sudo cp -r . /opt/ca-sentinel && cd /opt/ca-sentinel
sudo python3 -m venv .venv && sudo ./.venv/bin/pip install -r requirements.txt
sudo chown -R sentinel:sentinel /opt/ca-sentinel
sudo cp deploy/ca-sentinel.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ca-sentinel
journalctl -u ca-sentinel -f
```

**Host it near the origin.** The measured 330–930 ms round trips above were from India to
a Hostinger edge. A VPS in the same region cuts that to tens of milliseconds, which is the
single biggest latency win available — bigger than any poll-interval change.

### Operating notes

- A heartbeat line prints every 5 minutes with uptime, check/change counts and outbox
  depth, so a quiet bot is visibly a healthy one.
- `logs/hits.log` is an append-only record of every new address, separate from the
  rotating application log.
- Per-target exponential backoff with full jitter (2 s → 5 min cap) handles origin
  hiccups without hammering.
- Telegram `429`s are honoured via `retry_after`; sends are paced at ~1.2 s apart, under
  the ~20/min per-chat limit.

---

## 7. Adding another website

### Case A — the site is ordinary (no code at all)

Most sites need nothing but a config entry. The `generic` parser already handles swap
links, labelled text, code blocks and body matches:

```yaml
  - id: othersite
    parser: generic
    base_url: https://othersite.com
    enabled: true
    targets:
      - path: /
        interval: 10
      - path: /calls
        interval: 5
    discovery:
      crawl_links: true
      probe_routes: true
      ct_logs: true
```

Check what it would find before enabling it:

```bash
./.venv/bin/python run.py check https://othersite.com/calls --parser generic
```

### Case B — the site has its own markup conventions

Drop one file in `sentinel/sites/`. It is picked up automatically at startup; **no core
file is edited**.

```python
# sentinel/sites/othersite.py
from .base import SiteParser, register


@register
class OtherSiteParser(SiteParser):
    site_id = "othersite"                      # matches `parser:` in config.yaml

    ca_selectors = (                           # these get 0.98 confidence
        "#token-address",
        ".drop-card .mono",
    )

    route_hints = ("vip", "alpha", "drops")    # added to the probe wordlist

    extra_hosts = ("cdn.othersite.com",)       # extra crawlable hosts
```

Then set `parser: othersite` in `config.yaml`. That is the whole job — fetching, change
detection, validation, dedup, persistence, retry and delivery are inherited.

Override `parse()` only when the defaults genuinely are not enough (see
`sentinel/sites/suppoman.py` for a worked example that adds a title label):

```python
    def parse(self, result: FetchResult) -> list[Candidate]:
        tree = HTMLParser(result.body)
        found = []
        for selector in self.ca_selectors:
            found += extract.from_selector(tree, selector, result.url)
        found += extract.from_links(tree, result.url)
        return extract.dedupe_candidates(found)
```

Other hooks: `discover_links()` to change the crawl frontier, `should_watch()` to filter
discovered URLs.

### Case C — the site genuinely needs JavaScript

If a site renders its CA client-side, first check the Network tab for the JSON endpoint it
calls — hitting that API directly is faster and more robust than driving a browser, and
usually possible. Only if there is no such endpoint, add a browser-backed fetcher:

1. `pip install playwright && playwright install chromium`
2. Add a `fetch_mode: browser` branch that renders the page and returns a `FetchResult`
   with `body` set to `page.content()`.
3. Everything downstream — parsing, validation, dedup, delivery — is unchanged.

Budget 1–3 s per load and ~300 MB RAM per browser, and poll those targets on a much
longer interval than HTTP ones.

### Adding a chain

Add a validator to `sentinel/chains.py` (pattern + `validate_*`), register it in
`validate()`, add explorer links in `explorer_links()`, and extend `scan_text()` in
`extract.py`. Dedup, storage and delivery need no changes.

---

## 8. Tuning

| Setting | Default | Effect |
|---|---|---|
| `targets[].interval` | 2 s / 4 s | Detection latency. The dominant knob |
| `min_confidence` | 0.60 | Raise to 0.9 for anchored-selector hits only |
| `bootstrap` | `seed` | `alert` announces what is already live on first run |
| `jitter` | 0.15 | ±15% spread so targets do not fire in lockstep |
| `discovery.probe_delay` | 0.4 s | Gap between probe requests |
| `discovery.max_watched` | 50 | Ceiling on auto-added targets |

At a 2 s interval one target is about **43,000 requests/day** returning ~9 MB of 304s.
That is genuinely trivial bandwidth, but it is not a trivial request count — raise the
interval if you want to be more conservative, and keep `CONTACT_EMAIL` set so the site
owner can reach you.

---

## 9. Scope and responsible use

This tool reads **public, unauthenticated pages only**, the same ones any browser can load.
It does not attempt to get past authentication, CAPTCHAs, rate limits or any access
control, and it sends no credentials. Route probing only requests paths and records which
ones return a normal page. CT-log lookups read a public transparency log.

`/secretarea/` is not access-controlled — it is an unlinked public page carrying
`noindex,nofollow`. Note that `noindex` is a request not to index; this tool does not
publish or index anything, it forwards a change to your own chat. If the operator later
puts that page behind a login, **stop monitoring it** rather than trying to authenticate.

Alerts carry an explicit "Unverified — confirm the contract yourself" footer for a reason.
A detected address is a string scraped from a web page, not a vetted token: pages get
defaced, links get swapped, and this project's own testing turned up a published address
that had been corrupted by its own site's markup. Nothing here is financial advice.
