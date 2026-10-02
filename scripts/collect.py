#!/usr/bin/env python3
"""Laptop Deal Radar - daily collector.

Scans (a) Shopify refurb/ITAD resellers for warranted-refurb ANCHOR prices and
(b) Craigslist local listings for DEALS, classifies each listing against the
agent-fleet min spec, values it against a per-family/per-generation anchor
(delivered cost per unit, buy band, DOA tolerance), solves "N units within
$budget", bakes index.html + data/deals.json, and prints a report (stdout IS
the cron report).

Stdlib only. Safe under `no_agent=true` cron (no execute_code, no inline -c).

Usage:
    python scripts/collect.py                    # fetch, value, bake site
    python scripts/collect.py --dry-run          # same but writes nothing
    python scripts/collect.py --dry-run --dump   # + write data/_pool.json
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from statistics import median

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
TEMPLATE = os.path.join(ROOT, "scripts", "template.html")
OUT_HTML = os.path.join(ROOT, "index.html")
OUT_JSON = os.path.join(DATA_DIR, "deals.json")
ANCHOR_STATE = os.path.join(DATA_DIR, "anchors.json")
POOL_DUMP = os.path.join(DATA_DIR, "_pool.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# A working 8th-gen-or-newer laptop below this is an accessory, a bare chassis
# or a scam. Lots are exempt: a $150 3-pack is legitimately $50/unit.
MIN_PLAUSIBLE_UNIT_USD = 45.0
MAX_PLAUSIBLE_UNIT_USD = 3000.0
# A single unit this far under anchor is far more often misdescribed than a real
# bargain, so it is flagged for verification instead of celebrated.
SUSPECT_RATIO = 0.30

# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

# (display name, title regex, family, default CPU generation when the title
#  names no CPU - a Latitude 7490 IS 8th gen whether or not the seller says so)
MODEL_PATTERNS = [
    ("Dell Latitude 5420", r"latitude\s*5420", "latitude_5400", 11),
    ("Dell Latitude 5410", r"latitude\s*5410", "latitude_5400", 10),
    ("Dell Latitude 5400", r"latitude\s*5400", "latitude_5400", 8),
    ("Dell Latitude 5430", r"latitude\s*5430", "latitude_5400", 12),
    ("Dell Latitude 5490", r"latitude\s*5490", "latitude_5400", 8),
    ("Dell Latitude 5520", r"latitude\s*5520", "latitude_5500", 11),
    ("Dell Latitude 5590", r"latitude\s*5590", "latitude_5500", 8),
    ("Dell Latitude 7420", r"latitude\s*7420", "latitude_7400", 11),
    ("Dell Latitude 7410", r"latitude\s*7410", "latitude_7400", 10),
    ("Dell Latitude 7400", r"latitude\s*7400", "latitude_7400", 8),
    ("Dell Latitude 7490", r"latitude\s*7490", "latitude_7400", 8),
    ("Lenovo ThinkPad T480", r"thinkpad\s*t480", "thinkpad_t", 8),
    ("Lenovo ThinkPad T490", r"thinkpad\s*t490", "thinkpad_t", 8),
    ("Lenovo ThinkPad T14", r"thinkpad\s*t14", "thinkpad_t", 11),
    ("Lenovo ThinkPad L480", r"thinkpad\s*l480", "thinkpad_l", 8),
    ("Lenovo ThinkPad L490", r"thinkpad\s*l490", "thinkpad_l", 8),
    ("Lenovo ThinkPad L14", r"thinkpad\s*l14", "thinkpad_l", 11),
    ("Lenovo ThinkPad X1 Carbon", r"thinkpad\s*x1\s*carbon", "thinkpad_x1", 8),
    ("HP EliteBook 840", r"elitebook\s*840", "elitebook_800", 8),
    ("HP EliteBook 850", r"elitebook\s*850", "elitebook_800", 8),
    ("HP EliteBook 830", r"elitebook\s*830", "elitebook_800", 8),
    ("HP ProBook 450", r"probook\s*450", "probook_400", 8),
]

# HP's own platform generation is part of the model name and beats any guess:
# EliteBook 840 G5/G6 are 8th gen, G7 is 10th, G8 is 11th.
HP_PLATFORM_GEN = {"3": 7, "4": 7, "5": 8, "6": 8, "7": 10, "8": 11, "9": 12}
HP_PLATFORM_RE = re.compile(r"elitebook\s*\d{3}\s*g(\d)\b", re.I)

FAMILY_LABEL = {
    "latitude_5400": "Latitude 5400-series", "latitude_5500": "Latitude 5500-series",
    "latitude_7400": "Latitude 7400-series", "thinkpad_t": "ThinkPad T-series",
    "thinkpad_l": "ThinkPad L-series", "thinkpad_x1": "ThinkPad X1 Carbon",
    "elitebook_800": "EliteBook 8xx", "probook_400": "ProBook 4xx",
}

# anything matching this is a part, not a machine
PARTS_RE = re.compile(
    r"\b(battery|charger|ac adapter|power adapter|keyboard|palmrest|"
    r"hinge|lcd|screen assembly|bezel|trackpad|touchpad|motherboard|"
    r"system board|heatsink|thermal|fan|bottom case|base cover|"
    r"dock|docking station|replicator|memory module|ram stick|"
    r"hard drive only|caddy|carry bag|sleeve|case only|stand|"
    r"replacement|for parts|salvage|bare chassis)\b", re.I)

NOT_A_LAPTOP_RE = re.compile(
    r"\b(desktop|optiplex|elitedesk|prodesk|monitor|printer|chromebook|"
    r"tablet|ipad|iphone|server|rack)\b", re.I)

CPU_RE = re.compile(r"\b(?:i[3579]|core\s+i[3579])[-\s]?(\d{4,5})[a-z]{0,2}\d{0,2}\b", re.I)
# "Core i7-8th" / "i5 10th" - a generation written as an ordinal next to the brand
ORDINAL_CPU_RE = re.compile(r"\b(?:i[3579]|core\s+i[3579])[-\s]?(\d{1,2})(?:st|nd|rd|th)\b", re.I)
GEN_WORD_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s*(?:gen|generation)\b", re.I)
RYZEN_RE = re.compile(r"\bryzen\s*[3579]\s*(\d)\d{3}", re.I)
RAM_RE = re.compile(r"\b(\d{1,3})\s*gb\s*(?:of\s*)?(?:ram|ddr\d?|memory)\b", re.I)
# The Craigslist convention "8GB 256GB SSD": a GB figure immediately in front of a
# storage-tagged figure is the RAM. Without this, an 8GB machine parses as
# "RAM unknown" and slips past a 16GB minimum.
RAM_PAIR_RE = re.compile(
    r"\b(\d{1,3})\s*gb\b[\s/,|+]{0,4}(?=\d{1,4}\s*(?:gb|tb)\s*(?:ssd|nvme|hdd|hard|solid|pcie|m\.2))",
    re.I)
RAM_LOOSE_RE = re.compile(r"\b(\d{1,3})\s*gb\b(?=[^.]{0,18}\bram\b)", re.I)
STORE_RE = re.compile(r"\b(\d{1,4})\s*(gb|tb)\s*(?:ssd|nvme|solid\s*state|pcie|m\.2)\b", re.I)
STORE_ALT_RE = re.compile(r"\b(?:ssd|nvme|solid\s*state)[^\d]{0,12}(\d{1,4})\s*(gb|tb)\b", re.I)
LOT_RES = [
    re.compile(r"\blot\s*(?:of)?\s*(\d{1,2})\b", re.I),
    re.compile(r"\b(\d{1,2})\s*(?:x|pack|pcs|units?|machines?|laptops?)\b", re.I),
    re.compile(r"\bqty[:.\s]*(\d{1,2})\b", re.I),
]
VALID_RAM = (2, 4, 6, 8, 12, 16, 20, 24, 32, 40, 48, 64, 128)

# sample of what the filters threw away, surfaced in the report so the numbers
# can be argued with instead of trusted blindly
REJECT_SAMPLES = []


def note_reject(reason, title, price):
    if len(REJECT_SAMPLES) < 12:
        REJECT_SAMPLES.append({"reason": reason, "title": (title or "")[:90],
                               "price": price})


def cpu_gen(title):
    """Return (generation:int|None, cpu_label:str|None)."""
    m = CPU_RE.search(title)
    if m:
        num = m.group(1)
        if num.startswith("1") and len(num) >= 4:
            two = num[:2]
            gen = int(two) if two in ("10", "11", "12", "13", "14") else 11
        else:
            gen = int(num[0])
        if 4 <= gen <= 14:
            return gen, m.group(0).strip()
    m = ORDINAL_CPU_RE.search(title)
    if m:
        gen = int(m.group(1))
        if 4 <= gen <= 14:
            return gen, m.group(0).strip()
    m = GEN_WORD_RE.search(title)
    if m:
        gen = int(m.group(1))
        if 4 <= gen <= 14:
            return gen, "Gen %d" % gen
    m = RYZEN_RE.search(title)
    if m:
        # Ryzen 3xxx~8th, 4xxx~10th, 5xxx~11th, 6/7xxx~12th Intel-equivalent
        equiv = {"3": 8, "4": 10, "5": 11, "6": 12, "7": 12}.get(m.group(1))
        if equiv:
            return equiv, "Ryzen " + m.group(1) + "xxx"
    return None, None


def ram_gb(title):
    for rx in (RAM_RE, RAM_PAIR_RE, RAM_LOOSE_RE):
        m = rx.search(title)
        if m:
            v = int(m.group(1))
            if v in VALID_RAM:
                return v
    return None


def storage_gb(title):
    for rx in (STORE_RE, STORE_ALT_RE):
        m = rx.search(title)
        if m:
            v = int(m.group(1))
            if m.group(2).lower() == "tb":
                v *= 1024
            if 32 <= v <= 8192:
                return v
    return None


def lot_size(title):
    for rx in LOT_RES:
        m = rx.search(title)
        if m:
            v = int(m.group(1))
            if 2 <= v <= 20:
                return v
    return 1


def model_of(title):
    """Return (display_name, family, default_gen) or (None, None, None)."""
    for name, rx, family, dgen in MODEL_PATTERNS:
        if re.search(rx, title, re.I):
            if family == "elitebook_800":
                hp = HP_PLATFORM_RE.search(title)
                if hp and hp.group(1) in HP_PLATFORM_GEN:
                    return name, family, HP_PLATFORM_GEN[hp.group(1)]
            return name, family, dgen
    return None, None, None


def classify(title):
    """Parse a listing title. Returns (row, reject_reason); reason None = usable.

    Generation resolution order: the CPU model in the title if readable, then the
    model's own known generation (a Latitude 7490 is 8th gen whether or not the
    seller says so), then HP's platform designator. `gen_source` records which,
    so an inferred generation is never presented as a verified spec.

    A listing with a readable generation but no model we hold an anchor for is
    kept for display but returns reason 'no_model', which bars it from ever
    being a ranked buy candidate.
    """
    if PARTS_RE.search(title):
        return None, "parts"
    if NOT_A_LAPTOP_RE.search(title):
        return None, "not_a_laptop"
    name, family, dgen = model_of(title)
    title_gen, cpu_label = cpu_gen(title)
    if not name and not title_gen:
        return None, "unrecognised"
    if title_gen is not None:
        gen, gen_source = title_gen, "cpu"
    elif dgen is not None:
        gen, gen_source = dgen, "model"
    else:
        gen, gen_source = None, None
    row = {"cpu_gen": gen, "gen_source": gen_source, "cpu": cpu_label,
           "ram_gb": ram_gb(title), "storage_gb": storage_gb(title),
           "lot_size": lot_size(title)}
    if not name:
        row.update(model="unrecognised business laptop", family=None)
        return row, "no_model"
    row.update(model=name, family=family)
    return row, None


# --------------------------------------------------------------------------
# valuation
# --------------------------------------------------------------------------

BANDS = [(0.40, "steal"), (0.60, "good"), (0.80, "fair")]


def band_for(ratio, lot):
    """Buy band for a delivered/anchor ratio.

    An implausibly cheap SINGLE unit is flagged, not celebrated. Multi-unit lots
    are exempt: a 3-pack at -67% is real, a single working 5420 at -75% is not.
    """
    if ratio <= SUSPECT_RATIO and lot <= 1:
        return "suspect"
    for cut, name in BANDS:
        if ratio <= cut:
            return name
    return "pass"


def value_listing(listing, anchor, anchor_basis, cfg):
    """Fill in delivered-per-unit economics for one listing."""
    lot = listing["lot_size"] or 1
    unit_price = listing["price"] / lot
    unit_ship = (listing.get("shipping_usd") or 0.0) / lot
    charger = cfg.get("charger_cost_usd", 0) if listing.get("chargers_included") is False else 0
    delivered = unit_price + unit_ship + charger
    ratio = delivered / anchor if anchor else None
    ceiling = cfg.get("purchase_ceiling", {}).get("hard_pass_above_usd")
    listing.update(
        unit_price=round(unit_price, 2),
        unit_shipping=round(unit_ship, 2),
        charger_cost=round(charger, 2),
        delivered_per_unit=round(delivered, 2),
        anchor=anchor,
        anchor_basis=anchor_basis,
        ratio=round(ratio, 3) if ratio else None,
        band=band_for(ratio, lot) if ratio else "unknown",
        vs_anchor_pct=round((ratio - 1) * 100, 1) if ratio else None,
        # delivered cost per SURVIVING unit if exactly one unit arrives dead
        doa_per_unit=(round(delivered * lot / (lot - 1), 2) if lot > 1 else None),
        ceiling_breach=bool(ceiling and delivered > ceiling),
    )
    return listing


# --------------------------------------------------------------------------
# adapters
# --------------------------------------------------------------------------

def http_json(url, timeout=25, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 - adapters must fail soft
            last = e
            time.sleep(1.0)
    raise last


def scan_shopify(cfg, rejects):
    """Warranted-refurb rows. These both DEFINE the anchor and are buyable."""
    anchor_rows, results = [], []
    for entry in cfg.get("shopify_hosts", []):
        host, label = entry["host"], entry.get("label", entry["host"])
        n_ok = n_raw = 0
        try:
            for page in range(1, int(entry.get("pages", 3)) + 1):
                blob = http_json("https://%s/products.json?limit=250&page=%d" % (host, page))
                prods = blob.get("products", [])
                if not prods:
                    break
                for p in prods:
                    title = (p.get("title") or "").strip()
                    ptype = (p.get("product_type") or "").lower()
                    if "laptop" not in ptype and "notebook" not in ptype:
                        continue
                    avail = [v for v in p.get("variants", []) if v.get("available")]
                    if not avail:
                        continue
                    try:
                        price = min(float(v["price"]) for v in avail)
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    n_raw += 1
                    row, why = classify(title)
                    if row is None:
                        rejects[why] = rejects.get(why, 0) + 1
                        note_reject(why, title, price)
                        continue
                    row.update(
                        source=label, source_kind="shopify", title=title,
                        url="https://%s/products/%s" % (host, p.get("handle", "")),
                        price=price, region="ships", shipping_usd=0.0,
                        shipping_known=False, chargers_included=None)
                    anchor_rows.append(row)
                    n_ok += 1
                time.sleep(0.5)
            results.append(("[ok] %s: %d comparable laptops of %d available"
                            % (label, n_ok, n_raw), n_ok))
        except Exception as e:  # noqa: BLE001
            results.append(("[FAIL] %s: %s" % (label, str(e)[:70]), 0))
    return anchor_rows, results


CL_BASE = "https://sapi.craigslist.org/web/v8/postings/search/full"
# Craigslist row layout (verified 2026-10):
#   [0] posting id-ish  [1] area code  [2] ?  [3] numeric price  [4] "1:1~lat~lon"
#   [5] flags  [6] [age, hash]  [7] [n, ...image ids]  [8] [len, slug]
#   [9] [len, "$260"]  [10] title
CL_PRICE, CL_GEO, CL_SLUG, CL_TITLE = 3, 4, 8, 10


def scan_craigslist(cfg, rejects):
    cl = cfg.get("craigslist", {})
    batches = cl.get("batches", [1])
    queries = cl.get("queries", [])
    delay = float(cl.get("request_delay_s", 2.0))
    max_reqs = int(cl.get("max_requests", 24))
    rows, results, used = [], [], 0
    for q in queries:
        got = 0
        for b in batches:
            if used >= max_reqs:
                break
            url = ("%s?batch=%d-0-360-0-0&cc=US&lang=en&searchPath=sss&query=%s"
                   % (CL_BASE, b, urllib.parse.quote(q)))
            used += 1
            try:
                data = (http_json(url).get("data") or {})
            except Exception as e:  # noqa: BLE001
                results.append(("[warn] craigslist b%d q=%r: %s" % (b, q, str(e)[:40]), 0))
                continue
            region = (data.get("location") or {}).get("url") or "craigslist.org"
            for it in data.get("items") or []:
                try:
                    title = str(it[CL_TITLE]).strip()
                    price = float(it[CL_PRICE] or 0)
                    geo = str(it[CL_GEO])
                except (IndexError, TypeError, ValueError):
                    continue
                if not price or price < 20:
                    continue
                row, why = classify(title)
                if row is None:
                    rejects[why] = rejects.get(why, 0) + 1
                    note_reject(why, title, price)
                    continue
                unit = price / (row["lot_size"] or 1)
                if unit < MIN_PLAUSIBLE_UNIT_USD or unit > MAX_PLAUSIBLE_UNIT_USD:
                    rejects["implausible_price"] = rejects.get("implausible_price", 0) + 1
                    note_reject("implausible_price", title, price)
                    continue
                lat = lon = None
                m = re.search(r"~(-?\d+\.\d+)~(-?\d+\.\d+)", geo)
                if m:
                    lat, lon = float(m.group(1)), float(m.group(2))
                try:
                    slug = it[CL_SLUG][1]
                except (IndexError, TypeError):
                    slug = ""
                row.update(
                    source="Craigslist " + region.split(".")[0], source_kind="craigslist",
                    title=title, price=price, region=region.split(".")[0],
                    url="https://%s/search/sss?query=%s"
                        % (region, urllib.parse.quote(title[:60])),
                    lat=lat, lon=lon, shipping_usd=0.0, shipping_known=True,
                    chargers_included=False, slug=slug)
                rows.append(row)
                got += 1
            time.sleep(delay)
        results.append(("[ok] craigslist %r: %d listings" % (q, got), got))
    return rows, results, used


def dedupe(rows):
    """Drop the same machine re-listed across regions or sources."""
    seen, out, dropped = set(), [], 0
    for r in sorted(rows, key=lambda x: x["delivered_per_unit"]):
        key = (re.sub(r"[^a-z0-9]+", "", (r.get("title") or "").lower())[:45],
               round(r["price"], 2), r["lot_size"])
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(r)
    return out, dropped


# --------------------------------------------------------------------------
# anchors
# --------------------------------------------------------------------------

def build_anchors(anchor_rows, cfg, min_spec):
    """What a TESTED, WARRANTED machine of this family+generation costs.

    Granularity is the whole point. A 12th-gen Latitude 5521 at a premium
    reseller is not a benchmark for an 8th-gen Latitude 5490, and a median
    across an entire reseller catalogue is dominated by newer stock - which
    makes every real listing look like a 70%-off steal. So anchors key on
    (family, generation), require >=3 agreeing live observations, and otherwise
    fall back to the configured VERIFIED benchmark for that generation.
    """
    buckets = {}
    for r in anchor_rows:
        fam, gen = r.get("family"), r.get("cpu_gen")
        ram, sto = r.get("ram_gb"), r.get("storage_gb")
        if not fam or gen is None or not ram or ram < min_spec["ram_gb"]:
            continue
        if sto is None or not (128 <= sto <= 512):
            continue  # fleet config, not a 1TB workstation
        buckets.setdefault("%s|%d" % (fam, gen), []).append(r["price"])

    live = {k: {"min": round(min(v), 2), "n": len(v), "median": round(median(v), 2)}
            for k, v in buckets.items() if len(v) >= 3}

    fb_cfg = {k: v for k, v in cfg.get("anchor_fallback", {}).items() if not k.startswith("_")}

    def fb(gen_key):
        entry = fb_cfg.get(gen_key) or fb_cfg.get("default") or {"usd": 175, "note": ""}
        if isinstance(entry, (int, float)):
            entry = {"usd": float(entry), "note": ""}
        return float(entry["usd"]), entry.get("note", "")

    # The anchor is the BEST available warranted alternative for that tier: the
    # cheaper of the configured verified benchmark and the cheapest comparable
    # listing the live scan actually saw. Deliberately NOT a median across a
    # reseller catalogue - premium stock (a G8 i7 with 512GB) inflates that and
    # turns honest listings into fake 50%-off steals.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    families = {}
    for key, obs in sorted(live.items()):
        fam, _, gen_s = key.partition("|")
        try:
            gen = int(gen_s)
        except ValueError:
            continue
        cfg_usd, _note = fb(str(gen))
        families[key] = {
            "family": fam, "gen": gen, "label": FAMILY_LABEL.get(fam, fam),
            "usd": min(obs["min"], cfg_usd), "n": obs["n"], "live": True,
            "observed_min": obs["min"], "observed_median": obs["median"],
            "configured": cfg_usd,
            "basis": ("cheapest warranted observed $%.2f (%d listings) vs configured $%.2f - using the lower"
                      % (obs["min"], obs["n"], cfg_usd))}

    by_gen = {}
    for g in ("11", "10", "8", "default"):
        usd, note = fb(g)
        by_gen[g] = {"usd": usd, "note": note}

    return {"families": families, "by_gen": by_gen,
            "live_keys": sorted(live.keys()),
            "updated_utc": now}


def anchor_for(anchors, listing):
    """(usd, basis) for a listing: its own family+generation, else its generation."""
    fam, gen = listing.get("family"), listing.get("cpu_gen")
    if fam and gen is not None:
        hit = anchors["families"].get("%s|%d" % (fam, gen))
        if hit:
            return hit["usd"], hit["basis"]
    if gen is not None:
        key = str(gen) if str(gen) in anchors["by_gen"] else (
            "11" if gen >= 11 else ("10" if gen == 10 else "8"))
        hit = anchors["by_gen"].get(key)
        if hit:
            return hit["usd"], "configured benchmark (%s gen)" % key
    d = anchors["by_gen"].get("default") or {"usd": 175.0}
    return d["usd"], "default benchmark"


# --------------------------------------------------------------------------
# solver
# --------------------------------------------------------------------------

def solve(pool, want_units, budget):
    """Cheapest set of purchases reaching want_units inside budget.

    Taking the N cheapest qualifying units minimises total spend, so an
    ascending pick is optimal here, not a heuristic. Suspect listings and
    unrecognised models are excluded: this is a buy list, not a curiosity list.
    """
    scored = sorted(
        ((r["delivered_per_unit"], r) for r in pool
         if r["band"] in ("steal", "good", "fair") and r.get("family")),
        key=lambda t: t[0])

    picks, units, spent, seen = [], 0, 0.0, set()
    blocked_by_budget = False
    for per_unit, r in scored:
        if units >= want_units:
            break
        cost = per_unit * max(1, r["lot_size"])
        if spent + cost > budget:
            blocked_by_budget = True   # a real listing, priced out by the budget
            continue
        key = (r["source"], r.get("title"))
        if key in seen:
            continue
        seen.add(key)
        picks.append(r)
        units += r["lot_size"]
        spent += cost
    return {
        "want_units": want_units, "budget_usd": budget,
        "units_found": units, "spent_usd": round(spent, 2),
        "per_unit": round(spent / units, 2) if units else None,
        "feasible": units >= want_units,
        # WHICH constraint bit: an unmet ask must never blame the budget when the
        # budget was not the binding limit (there were simply too few listings).
        "blocked_by_budget": bool(blocked_by_budget and units < want_units),
        "candidates_considered": len(scored),
        "picks": picks,
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    dry = "--dry-run" in sys.argv
    dump = "--dump" in sys.argv
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    min_spec = cfg["min_spec"]
    rejects = {}
    del REJECT_SAMPLES[:]
    report = []

    anchor_rows, res1 = scan_shopify(cfg, rejects)
    anchors = build_anchors(anchor_rows, cfg, min_spec)
    cl_rows, res2, cl_used = scan_craigslist(cfg, rejects)
    for line, _n in res1 + res2:
        report.append(line)

    pool = []
    for r in anchor_rows + cl_rows:
        gen, ram, sto = r.get("cpu_gen"), r.get("ram_gb"), r.get("storage_gb")
        if ((gen is not None and gen < min_spec["cpu_gen"])
                or (ram is not None and ram < min_spec["ram_gb"])
                or (sto is not None and sto < min_spec["storage_gb"])):
            rejects["below_min_spec"] = rejects.get("below_min_spec", 0) + 1
            continue
        r["spec_complete"] = all(v is not None for v in (gen, ram, sto))
        usd, basis = anchor_for(anchors, r)
        value_listing(r, usd, basis, cfg)
        pool.append(r)

    pool, dupes = dedupe(pool)
    if dupes:
        rejects["duplicate"] = dupes

    pool.sort(key=lambda x: x["delivered_per_unit"])
    rankable = [r for r in pool if r.get("family")]
    # qualifying listings first, then the rest of the market for context - a thin
    # day must still show what the board actually looks like
    top = sorted(rankable, key=lambda x: (x["band"] == "pass", x["delivered_per_unit"]))
    top = top[: int(cfg.get("max_results", 25))]

    solution = solve(pool, int(cfg.get("want_units", 1)), float(cfg.get("budget_usd", 0)))

    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "config": {k: v for k, v in cfg.items() if not k.startswith("_")},
        "anchors": anchors,
        "family_labels": FAMILY_LABEL,
        "source_health": [{"line": l, "n": n} for l, n in res1 + res2],
        "counts": {
            "anchor_rows": len(anchor_rows), "craigslist_rows": len(cl_rows),
            "craigslist_requests": cl_used, "qualifying": len(pool),
            "rankable": len(rankable),
        },
        "rejects": rejects,
        "reject_samples": list(REJECT_SAMPLES),
        "solution": solution,
        "deals": top,
    }

    report.append("")
    report.append("anchors (tested + warranted benchmarks):")
    for key in anchors["live_keys"]:
        f = anchors["families"][key]
        report.append("  live %-22s $%7.2f  %s" % (key, f["usd"], f["basis"]))
    for g in ("11", "10", "8"):
        b = anchors["by_gen"][g]
        report.append("  cfg  %-22s $%7.2f  %s" % (g + "th gen", b["usd"], b["note"] or "benchmark"))
    report.append("scanned: %d anchor rows + %d craigslist rows (%d requests) -> %d qualifying"
                  % (len(anchor_rows), len(cl_rows), cl_used, len(pool)))
    if rejects:
        report.append("filtered: " + ", ".join("%s=%d" % kv for kv in sorted(rejects.items())))
    if REJECT_SAMPLES:
        report.append("sample of what was filtered out:")
        for s in REJECT_SAMPLES[:5]:
            report.append("  %-18s $%-8s %s" % (s["reason"], s["price"], s["title"][:66]))

    if top:
        report.append("")
        report.append("top deals (delivered per unit vs its own anchor):")
        for r in top[:8]:
            report.append("  %-7s $%7.2f/u %6.1f%%  %-26s lot%-2d %s"
                          % (r["band"], r["delivered_per_unit"], r["vs_anchor_pct"],
                             r["model"][:26], r["lot_size"], r["source"]))
    else:
        report.append("")
        report.append("top deals: none qualified this run")

    report.append("")
    s = solution
    if s["feasible"]:
        report.append("ASK SOLVED: %d units for $%.2f ($%.2f/unit) inside $%.0f"
                      % (s["units_found"], s["spent_usd"], s["per_unit"], s["budget_usd"]))
    elif s["units_found"] and s.get("blocked_by_budget"):
        report.append("ASK PARTIAL: %d of %d units for $%.2f ($%.2f/unit); the $%.0f budget priced out the rest"
                      % (s["units_found"], s["want_units"], s["spent_usd"],
                         s["per_unit"], s["budget_usd"]))
    elif s["units_found"]:
        report.append("ASK NOT MET: %d of %d units for $%.2f ($%.2f/unit) - budget was NOT the constraint,"
                      " only %d listing(s) banded better than pass"
                      % (s["units_found"], s["want_units"], s["spent_usd"],
                         s["per_unit"], s.get("candidates_considered", 0)))
    elif s.get("blocked_by_budget"):
        report.append("ASK UNSOLVED: nothing affordable inside $%.0f" % s["budget_usd"])
    else:
        report.append("ASK UNSOLVED: no listing on this run banded better than pass (%d considered)"
                      % s.get("candidates_considered", 0))

    if dry:
        if dump:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(POOL_DUMP, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            report.append("dumped: data/_pool.json")
        print("\n".join(report))
        print("\n[dry-run] site not written")
        return 0

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    with open(ANCHOR_STATE, "w", encoding="utf-8") as fh:
        json.dump(anchors, fh, indent=1)

    with open(TEMPLATE, encoding="utf-8") as fh:
        tpl = fh.read()
    baked = tpl.replace("/*__DATA__*/null",
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if "/*__DATA__*/" in baked:
        print("[FAIL] template placeholder not substituted - refusing to ship")
        return 1
    with open(OUT_HTML, "w", encoding="utf-8") as fh:
        fh.write(baked)

    report.append("baked: index.html (%d bytes) + data/deals.json" % os.path.getsize(OUT_HTML))
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
