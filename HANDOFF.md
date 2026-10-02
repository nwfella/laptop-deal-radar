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

## Current verified state (2026-10-01)

- `python scripts/collect.py` → exit 0. 98 anchor rows + 80 Craigslist rows across 32 requests.
- `node scripts/verify_site.js` → **37 assertions, exit 0**. This gate has caught 3 real bugs; never bypass it.
- Cron end-to-end proven: collect → verify → deploy → CDN serving the deployed commit byte-identical.

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
