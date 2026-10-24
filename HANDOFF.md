# HANDOFF — Laptop Deal Radar

**Read this first in a fresh session.** Then `~/projects/laptop-deal-radar/README.md`, and load the
`used-laptop-agent-fleet` skill (its `references/laptop-deal-source-scrapability.md` has the verified
endpoint map). Those three files carry the durable state; this is a pointer map, not a retelling.

## What it is

A daily scanner that answers: *what is the cheapest way to get N used business laptops for an agent
fleet inside a $ budget, and is any given listing actually a good deal?* Deliberately strict — most
days it correctly reports that nothing beats the benchmark.

| | |
|---|---|
| Repo | https://github.com/nwfella/laptop-deal-radar (public; `main` = source, `gh-pages` = artifact) |
| Live | https://nwfella.github.io/laptop-deal-radar/ |
| Local | `~/projects/laptop-deal-radar` (pages clone at `~/projects/laptop-deal-radar-pages`) |
| Cron | `7c70756fcd22` `laptop-deal-radar-daily`, `no_agent`, daily 09:00, `deliver=origin` |
| Cost | **$0** — script-only, zero model calls. 32 Craigslist + 8 Shopify requests per run, ~160s |

## Current verified state (2026-10-24)

- `python scripts/collect.py` → exit 0. 89 anchor rows + 78 Craigslist rows across 32 requests.
- `node scripts/verify_site.js` → **44 assertions, exit 0**. This gate has caught 7 real bugs; never bypass it.
- Cron end-to-end proven: collect → verify → deploy → CDN serving the deployed commit byte-identical.

### Lots are accepted, and lot prices are not what Craigslist's price field says (2026-10-24)

Two bugs stood between the ask and a multi-unit lot:

1. **`+Dock` made a 3-pack look like a dock.** `PARTS_RE` rejected any title containing
   `dock|battery|charger|keyboard|fan|stand`, so "Lot of 3 ThinkPad X1 Carbon 11th gen i5 16/512GB
   + Dock" was binned as `parts` — three laptops with a bundled docking station, thrown away by the
   accessory. Split into `PARTS_ALWAYS_RE` (unambiguous: palmrest, system board, cpu, screen assembly…)
   and `PARTS_AMBIGUOUS_RE` (battery/charger/keyboard/dock/fan/stand), where an ambiguous word only
   means "this listing *is* the part" when the title shows no explicit CPU gen / RAM / storage — the
   model's *known* generation does not count, or every "Latitude 7490 Battery" would pass.
2. **A lot's price field may be per-unit.** The first lot the fix surfaced read
   "Pricing is set at **$295 per unit**, or **$250 each** when purchased as a complete set of three".
   Taken as the lot price that is $128.33/unit and a fake `ASK SOLVED: 3 units for $384.99`; the real
   delivered cost is $325/unit. `PER_UNIT_RE` now detects per-unit language in the title **and** in the
   ad body and multiplies by `lot_size` before valuation, recording `price_basis` (`lot` |
   `per_unit_xN`), which the table prints. The ad's bundle discount is deliberately *not* applied —
   conservative by design.

Verified: classifier 12/12 on planted cases (real lots accepted, battery/charger/keyboard/dock/fan
listings rejected), 44 assertions green, live == blob.

**Outcome on 2026-10-24:** accepting lots did not buy a third unit. The three X1 Carbon lots on the
board deliver at $180, $195 and $325 per unit — all `pass` and all above the $175 ceiling. The ask
stays honestly `2 of 3 units for $370` until a lot lands under ~$145/unit.

### Condition screen: read the ad body before calling something a deal (2026-10-24)

The board's best pick was an HP EliteBook 840 G11 at $100, −46% of anchor, band `good`. Its ad said
`condition: salvage`, "The SSD and RAM have been removed as the mainboard is Secure Boot/ BIOS
Locked", and the seller had lost the BIOS password — HP's only quoted fix was a new mainboard. It sat
in Craigslist's **computer parts** category. The real cost is not $100: it is a brick.

Nothing about price, model or geo can see that, and the search API returns **no description**. So:

1. **Titles are screened for free** (`CONDITION_HARD`): bios locked/issue/password, RAM/SSD
   removed/pulled/missing, for-parts/salvage/not working, computer-parts category, Computrace /
   Autopilot / Intune / activation lock. `CONDITION_SOFT` ("read description", "as-is") is not a
   rejection, just a reason to read the body.
2. **Every buy candidate's body is fetched** (`screen_conditions`) — rows with a known family and
   band ≤ fair, capped by `config.condition_screen_max` (20). Hard flags drop the row and land in the
   reject ledger as `condition:<flag>`. Rows beyond the cap or with an unreadable body are published
   marked `⚠ condition unread` — unknown is never treated as fine.
3. **The body's own specs override the title** (`posting_description` → `ram_gb`/`storage_gb`), and a
   body-stated spec below `min_spec` disqualifies the unit exactly like a title one. This caught a
   second bogus pick in the same run: "Lenovo Latitude 7490 Laptop", $150, title gave no RAM, body
   said **8GB DDR4**. Dropping it is why the ask now reads `ASK NOT MET: 2 of 3 units` — honest.

Three assertions guard it (no published buy candidate carries a condition flag, every Craigslist buy
candidate was body-read, none sits below min_spec on a known field). Verified RED against the previous
bake (1 failure at 40) before going green at 42.

### Deal links are real permalinks (fixed 2026-10-20)

Craigslist deals used to publish `https://<region>/search/sss?query=<title[:60]>` — a synthesised
search URL, not the post. Every "link" on the live page therefore opened Craigslist saying *no
listing found*, which reads as a dead listing while the post is live. The sapi row already carries
everything needed at no extra request:

```
https://www.craigslist.org/view/d/<item[8][1] slug>/<item[6][1] token>
```

Byte-identical to the href the Craigslist HTML SERP emits for that row, 200 on all 25 deals. Two
assertions now guard it (no `/search/` stub in the payload; every Craigslist deal matches the
permalink regex), plus the DOM row-link check rejects a `/search/` href. Verified RED against the
previous bake (3 failures) before the fix went green at 39.

## The ask (edit `config.json`, re-run)

```json
"want_units": 3, "budget_usd": 700, "charger_cost_usd": 30,
"min_spec": { "cpu_gen": 8, "ram_gb": 16, "storage_gb": 256 },
"purchase_ceiling": { "hard_pass_above_usd": 175 }
```

Bands vs anchor: steal ≤40%, good ≤60%, fair ≤80%, else pass. A single unit ≤30% of anchor is
flagged `suspect` (verify it's a whole machine); multi-unit lots are exempt.

## Anchors (refit 2026-10-01 — evidence in `config.json` per tier)

| Tier | Anchor | Weakness |
|---|---|---|
| 11th gen | $204 | strongest — two sellers agree, corroborated upward by eBay $235 / Back Market $273 |
| 10th gen | $245 | **weakest** — one model, snippet-sourced |
| 8th gen | $240 | two prices page-verified (Alamogeeks $249.98, Woot $269.99) |

These were $204/$180/$160 before the refit. The old 8th/10th values came from used-lot street prices,
a different market, and were ~40% too low — which pushed everything into `pass`.

## Open work, in order

1. **eBay Browse API adapter.** Client ID + secret only; `client_credentials` grant, scope
   `https://api.ebay.com/oauth/api_scope`, app token valid ~2h so mint per run. No user auth, no
   refresh token. Free tier 5,000 calls/day. **Credentials go in env vars `EBAY_CLIENT_ID` /
   `EBAY_CLIENT_SECRET` — never paste them in chat.** See "Credentials" below.
2. **Make anchor divergence visible.** `min(configured, observed)` is a one-way ratchet: live data
   can only lower an anchor, never raise it, and today observed is +96% to +179% above configured so
   the live scan has *zero* effect. Flag a tier on the page when configured and observed disagree by
   more than ~40%, instead of silently resolving it.
3. **Add the value sellers** (LaptopReno, GotLaptopParts — the actual $204 source). Not Shopify, so
   they need a small custom adapter; adding them makes the live anchor meaningful.
4. **`~/projects-summary/curation.json`** — this project has no index row yet. Deliberately left to you
   because the index's Jev column is your judgement call.

## Credentials

The eBay secret must not enter the conversation. Anything pasted in chat lands in the provider's
request logs, the session transcript, and any summary derived from it. Options verified present on
this install:

- `hermes secrets` — pulls keys from Bitwarden Secrets Manager / 1Password at process startup instead
  of storing them in `~/.hermes/.env`.
- **Windows user environment variable** (recommended here) — you set it, the collector reads
  `os.environ`. No new software, works under cron.
- `hermes egress` (iron-proxy) — strongest in principle, swaps proxy tokens for real credentials
  before egress. But it installs a **TLS-intercepting CA** on the machine; on monitored/IT-constrained
  work PCs that is exactly the kind of change IT notices. Not recommended for this host.

The repo is **public**, so env vars are mandatory rather than merely preferable. Blast radius is small
by design: the Browse API app token can only search public listings — no account access, no money, and
the secret is regenerable.

## Gotchas that already cost time (full detail in README "Accuracy notes")

- Anchor must be the cheapest **tested, warranted** machine of that family *and* generation — never a
  catalogue median (premium stock faked 50%-off steals at $736.77) and never a used-lot street price.
- Resolve generation as: CPU in title → the model's own known generation → HP's platform code
  (`840 G5`=8th, `G7`=10th, `G8`=11th). `gen_source` records which; the page marks inferred ones `gen8*`.
- `i7-1185G7` breaks a naive CPU regex; `8GB 256GB SSD` is the Craigslist RAM convention.
- Never blame the budget when the budget wasn't the binding constraint — `solution.blocked_by_budget`
  distinguishes a thin market from a real budget limit. There is a verify assertion guarding this.

## Run / verify

```bash
cd ~/projects/laptop-deal-radar
python scripts/collect.py                  # bake
python scripts/collect.py --dry-run --dump # write nothing, dump data/_pool.json
node scripts/verify_site.js                # gate - must pass before deploy
python scripts/daily_refresh.py            # collect + gate + deploy to gh-pages
```

**Pages propagation lag is real (~40-60s).** An immediate readback of the live URL after a deploy can
still serve the *previous* commit. Compare `git show origin/gh-pages:index.html` against the live
response and retry before concluding a deploy failed — verified twice on 2026-10-01.
