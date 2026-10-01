/* Laptop Deal Radar - jsdom boot + render verification.
 *
 * Loads the BAKED index.html with scripts enabled and asserts the render path
 * actually works on real baked data: the payload parses, the ask banner
 * resolves, tiles/cards render, every deal row links out, bands and dollars are
 * well formed, anchors and source health resolve, the reject ledger is present,
 * and no boot error occurred.
 *
 * Usage:  npm i -D jsdom  &&  node scripts/verify_site.js
 * Exit code 0 = pass. Non-zero = do not deploy.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const file = path.resolve(__dirname, "..", "index.html");
if (!fs.existsSync(file)) {
  console.error("FAIL: index.html not found at " + file + " - run scripts/collect.py first");
  process.exit(1);
}
const html = fs.readFileSync(file, "utf8");
const fails = [];
const info = [];
const ok = (cond, msg) => (cond ? info.push("  ok   " + msg) : fails.push("  FAIL " + msg));

// ---- 1. the baked payload must be valid JSON, not just substituted text ----
const m = html.match(/const DATA = (\{[\s\S]*?\});\s*\n\(function/);
ok(!!m, "baked payload block present in index.html");
let data = null;
if (m) {
  try {
    data = JSON.parse(m[1]);
    ok(true, "baked payload parses as JSON");
  } catch (e) {
    ok(false, "baked payload is not valid JSON: " + e.message);
  }
}
if (data) {
  ok(typeof data.generated_utc === "string" && data.generated_utc.length > 8,
    "payload carries a generation timestamp");
  ok(Array.isArray(data.deals), "payload.deals is an array");
  ok(data.solution && typeof data.solution.want_units === "number",
    "payload.solution carries the units ask");
  ok(data.anchors && data.anchors.by_gen && data.anchors.by_gen["11"],
    "payload.anchors.by_gen has an 11th-gen benchmark");
  ok(Array.isArray(data.source_health) && data.source_health.length >= 2,
    "payload reports health for >=2 sources");
  // refunds / sanity: a deal must never claim a sub-$45 single unit
  const bad = (data.deals || []).filter(
    (r) => r.lot_size <= 1 && r.price / (r.lot_size || 1) < 45);
  ok(bad.length === 0, "no single-unit listing under $45 was published as a deal");
  // every published deal must carry the anchor it was judged against
  const noAnchor = (data.deals || []).filter((r) => !r.anchor || !r.anchor_basis);
  ok(noAnchor.length === 0, "every published deal carries its anchor and basis");
  // a deal whose model is known must resolve a real generation, not the generic
  // $175 default - otherwise the whole tier comparison silently degrades
  const noGen = (data.deals || []).filter((r) => !r.cpu_gen);
  ok(noGen.length === 0, "every published deal resolved a CPU generation (" + noGen.length + " unknown)");
  const defaultAnchor = (data.deals || []).filter((r) => /default benchmark/.test(r.anchor_basis || ""));
  ok(defaultAnchor.length === 0,
    "no published deal fell back to the generic default anchor (" + defaultAnchor.length + ")");
  const inferred = (data.deals || []).filter((r) => r.gen_source === "model").length;
  ok((data.deals || []).every((r) => r.gen_source === "cpu" || r.gen_source === "model"),
    "generation provenance recorded on every deal (" + inferred + " inferred from model)");
}

// ---- 2. boot the page ----
const vc = new VirtualConsole();
const errors = [];
vc.on("jsdomError", (e) => errors.push(String(e && e.message)));
vc.on("error", (msg) => errors.push(String(msg)));

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "https://nwfella.github.io/laptop-deal-radar/",
  virtualConsole: vc,
  beforeParse(window) {
    window.matchMedia = window.matchMedia || (() => ({
      matches: false, addListener() {}, removeListener() {},
      addEventListener() {}, removeEventListener() {},
    }));
  },
});
const doc = dom.window.document;
const $ = (id) => doc.getElementById(id);

ok(errors.length === 0, "no boot error" + (errors.length ? ": " + errors[0].slice(0, 120) : ""));

// ---- 3. header ----
const stamp = ($("stamp") || {}).textContent || "";
ok(stamp.indexOf("loading") === -1 && stamp.indexOf("data as of") === 0,
  "header stamp shows the data timestamp");

// ---- 4. ask banner ----
const askTitle = ($("ask-title") || {}).textContent || "";
ok(/\d+\s+of\s+\d+\s+units/.test(askTitle), "ask banner resolves N of M units");
const askVerdict = ($("ask-verdict") || {}).textContent || "";
ok(askVerdict.length > 40 && askVerdict !== "—", "ask banner renders a verdict sentence");
const askSub = ($("ask-sub") || {}).textContent || "";
ok(askSub.indexOf("budget $") !== -1, "ask banner shows the budget");

// ---- 5. tiles ----
const tiles = doc.querySelectorAll("#stat-tiles .tile");
ok(tiles.length >= 6, "at least 6 stat tiles rendered (got " + tiles.length + ")");
const tileText = Array.prototype.map.call(tiles, (t) => t.textContent).join(" | ");
ok(tileText.indexOf("qualifying") !== -1, "tiles include the qualifying count");
ok(tileText.indexOf("anchor") !== -1, "tiles include anchor benchmarks");

// ---- 6. deals table ----
const rows = doc.querySelectorAll("#deals-body tr");
const dealsCount = ($("deals-count") || {}).textContent || "";
ok(dealsCount.indexOf("shown") !== -1, "deals heading reports a count");
ok(rows.length > 0, "at least one deal row rendered (got " + rows.length + ")");
if (data && data.deals) {
  ok(rows.length === data.deals.length,
    "rendered row count matches payload deal count (" + rows.length + " vs " + data.deals.length + ")");
}
const BANDS = ["steal", "good", "fair", "pass", "suspect", "unknown"];
let linkOk = 0, bandOk = 0, moneyOk = 0, badLink = null;
Array.prototype.forEach.call(rows, (tr) => {
  const a = tr.querySelector("a");
  if (a && /^https:\/\//.test(a.getAttribute("href"))) linkOk++;
  else badLink = badLink || (a ? a.getAttribute("href") : "(no anchor)");
  const chip = tr.querySelector(".band");
  if (chip && BANDS.indexOf(chip.className.replace("band", "").trim()) !== -1) bandOk++;
  const nums = Array.prototype.filter.call(tr.querySelectorAll("td.num"),
    (td) => /\$\d/.test(td.textContent));
  if (nums.length >= 2) moneyOk++;
});
ok(rows.length === 0 || linkOk === rows.length,
  "every deal row links out (" + linkOk + "/" + rows.length + ")" + (badLink ? " bad: " + badLink : ""));
ok(rows.length === 0 || bandOk === rows.length, "every deal row has a valid band chip");
ok(rows.length === 0 || moneyOk === rows.length, "every deal row shows dollar figures");

// ---- 7. anchors: benchmarks always present, live family anchors optional ----
const arows = doc.querySelectorAll("#anchor-body tr");
ok(arows.length === 3, "generation benchmark table renders all 3 tiers (got " + arows.length + ")");
const anchorText = Array.prototype.map.call(arows, (t) => t.textContent).join(" ");
ok(/verified|estimated|benchmark/.test(anchorText),
  "each generation benchmark states its basis (verified / estimated)");
ok(/\$\d/.test(anchorText), "generation benchmarks carry dollar values");
const famRows = doc.querySelectorAll("#family-body tr");
const famEmptyShown = ($("family-empty") || {}).style.display !== "none";
ok(famRows.length > 0 || famEmptyShown,
  "live family anchors render, or an explicit empty state explains why not");
if (data && data.anchors) {
  ok(famRows.length === Object.keys(data.anchors.families || {}).length,
    "live family anchor rows match the payload (" + famRows.length + ")");
}

// ---- 8. filtered ledger + source health ----
ok(doc.querySelectorAll("#rejects .rj").length >= 1, "reject ledger renders");
const health = doc.querySelectorAll("#source-health li");
ok(health.length >= 2, "source health lists >=2 sources (got " + health.length + ")");

// ---- 9. suspect rule documented on the page ----
const foot = (doc.querySelector("footer") || {}).textContent || "";
ok(foot.indexOf("suspect") !== -1, "footer documents the suspect-single rule");
ok(foot.indexOf("1 dead") !== -1, "footer documents the one-dead-unit figure");

dom.window.close();

console.log(info.join("\n"));
if (fails.length) {
  console.log(fails.join("\n"));
  console.error("\nVERIFY FAILED: " + fails.length + " assertion(s)");
  process.exit(1);
}
console.log("\nVERIFY OK: " + info.length + " assertions passed");
process.exit(0);
