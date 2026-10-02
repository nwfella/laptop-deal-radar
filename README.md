# Laptop Deal Radar

A daily scanner that answers one question: **what is the cheapest way to get N used business
laptops for an agent fleet, inside a $ budget — and is any given listing actually a good deal?**

It is deliberately strict. Most days it reports that nothing on the board beats the benchmark
price, and that is the tool working: it is the same discipline that made a 3-for-$200 Latitude 5420
lot worth buying and a $200-each listing worth walking away from.

Live page: <https://nwfella.github.io/laptop-deal-radar/>

## How a listing is valued

```
delivered $/unit = (asking price ÷ lot size) + shipping share + charger cost (when not included)
ratio            = delivered $/unit ÷ anchor
```

| Band | Ratio vs anchor | Reading |
|---|---|---|
| steal | ≤ 40% | buy immediately, verify it is a whole machine |
| good | ≤ 60% | buy |
| fair | ≤ 80% | acceptable if the spec is right |
| pass | > 80% | you are paying retail without retail's benefits |
| suspect | ≤ 30% **and a single unit** | verify — at that price it is usually a battery, a bare chassis or a misdescription |

Multi-unit lots are exempt from `suspect`: a 3-pack at −67% is real, a single working laptop at −75% is not.

**1 dead** is the delivered cost per surviving unit if exactly one machine arrives non-working. It is
the honest risk number for a no-warranty lot, and it is what a budget should be checked against.

## What "anchor" means

The anchor is the **best available warranted alternative** for that family and generation — what it
costs to buy the same class of machine tested, warranted and returnable. A listing is only a deal if
it meaningfully undercuts that.

Anchors are built as `min(configured benchmark, cheapest comparable live listing)`, per
(family, generation). Two hard-won details:

- **Not a median across a reseller catalogue.** Reseller stock is dominated by newer, higher-spec
  machines; a catalogue-wide median put the anchor for an 8th-gen Latitude at $399 and made every
  honest listing look like a 70%-off steal.
- **Not a mean across spec levels.** The median of live 11th-gen EliteBooks came out at $736.77
  because the sampled units were premium i7/512GB configs — enough to fake a 50%-off steal on
  every ordinary G8. Taking the cheapest comparable listing, against the configured benchmark,
  gives the defensible $204.

The configured benchmarks in `config.json` are the fallback and are labelled *verified* or
*estimated* on the page. They are what applies when the resellers do not stock the model this week.

Refit 2026-10-01, with the evidence recorded per tier in `config.json`:

| Tier | Anchor | Basis |
|---|---|---|
| 11th gen | **$204** | LaptopReno + GotLaptopParts both list a Latitude 5420 i5 16/256 at $203.99 |
| 10th gen | **$245** | ThinkPad T14 Gen 1 i5-10310U 16/256: Wisetek Market $243.71, Newegg $244.70 |
| 8th gen | **$240** | page-verified Alamogeeks 5400 $249.98 and Woot 7490 $269.99; Newegg 7490 $219.00 + $19.99 ship |

The previous 8th/10th-gen values ($160/$180) were **~40% too low**: they were carried over from
the skill's used-lot street prices, which are a different market from tested, warranted, returnable
units. That error made the tool over-conservative — low anchors push listings into `pass`.

**Two caveats on the current numbers.** The tiers are not yet on a consistent channel basis (the
11th-gen figure comes from high-volume value sellers, the 8th/10th from retail channels), which is
why 11th gen currently prices *below* 8th gen. And the 10th-gen tier rests on a single model from
search snippets. Adding the eBay Browse API fixes both by putting every tier on one live basis.

## Sources

| Source | Adapter | What it contributes |
|---|---|---|
| Shopify refurb/ITAD resellers (`/products.json`) | `scan_shopify` | warranted-refurb anchor prices, plus buyable listings |
| Craigslist (`sapi.craigslist.org` search JSON) | `scan_craigslist` | local-pickup deals, no shipping tax — where the cheap lots actually are |

Craigslist `batch=N` walks US regions in sequence (`1`=sfbay, `2`=seattle, `3`=newyork, `4`=boston,
`7`=losangeles, `8`=sandiego). Batches that answer 400 are skipped.

## Configuration

Everything user-facing is in `config.json`:

```json
{
  "want_units": 3,                    // how many machines you are buying
  "budget_usd": 700,                  // total, delivered, incl. shipping and chargers
  "charger_cost_usd": 30,             // added when a listing does not include one
  "min_spec": { "cpu_gen": 8, "ram_gb": 16, "storage_gb": 256 },
  "max_results": 25,
  "purchase_ceiling": { "hard_pass_above_usd": 175 },
  "anchor_fallback": { "11": { "usd": 204, "note": "verified Oct 2026: ..." } },
  "shopify_hosts": [ ... ],
  "craigslist": { "batches": [1,2,7,8], "queries": [ ... ], "max_requests": 34 }
}
```

`want_units` + `budget_usd` are the whole interface. Taking the N cheapest qualifying units
minimises total spend, so the solver is a plain ascending pick — optimal, not a heuristic. Lots of
more than one unit count as their lot size.

## Running it

```bash
python scripts/collect.py                  # fetch, value, bake index.html + data/deals.json
python scripts/collect.py --dry-run        # write nothing
python scripts/collect.py --dry-run --dump # + data/_pool.json for inspection
node scripts/verify_site.js                # jsdom gate - must pass before deploy
```

`collect.py` is stdlib-only and safe under `no_agent=true` cron (no `execute_code`, no inline `-c`).
Its **stdout is the report** — that is what the cron job delivers.

## Cron

`~/.hermes/scripts/laptop-deal-radar-daily.py` is a thin launcher into `scripts/daily_refresh.py`,
which runs the collector, gates on the jsdom verify, and pushes to the `gh-pages` branch only when
the artifact changed. Job: `no_agent=true`, `script=laptop-deal-radar-daily.py`, `workdir=<repo>`.

## Accuracy notes worth keeping

These cost real debugging time; do not regress them.

- **Generation resolution order** is CPU model in title → the model's own known generation → HP's
  platform designator (`840 G5`=8th, `G7`=10th, `G8`=11th). `gen_source` records which was used, and
  the page marks an inferred generation `gen8*` so it is never presented as a seller-verified spec.
  Before this, 21 of 25 published listings had *no* generation and silently fell back to the generic
  $175 anchor, which inverts the tier comparison.
- **`i7-1185G7` needs a permissive CPU regex.** Intel's `G7` suffix broke a naive `(\d{4,5})[a-z]{0,2}`
  pattern, so the whole model failed to parse and the generation came back unknown.
- **"8GB 256GB SSD" is the Craigslist RAM convention.** Without the pair pattern, an 8GB machine
  parses as "RAM unknown" and slips past a 16GB minimum.
- **Parts are the main false positive.** `PARTS_RE` rejects batteries, chargers, sleeves, caddies;
  the reject ledger on the page lists what was dropped so the filtering can be argued with.

## Limitations

- Craigslist posting links are region searches, not permalinks — the row payload has no usable
  posting id, so each link opens that region's search for the listing's own title.
- Shopify shipping is assumed free (`shipping_known: false`); chargers are assumed absent on
  Craigslist (`chargers_included: false`) and costed at `charger_cost_usd`.
- The anchor reflects *this week's* reseller stock. `data/anchors.json` records what each run saw.
- Nothing here reserves inventory or verifies a seller's condition claim.

## Licence

MIT.
