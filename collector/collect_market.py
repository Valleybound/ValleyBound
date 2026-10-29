#!/usr/bin/env python3
"""Hourly market snapshot collector for Valleybound.

Fetches the Pax Dei community market feed (paxdei.gaming.tools — the
unofficial public feed approved by the developers, refreshed hourly) and
aggregates it into Valleybound's snapshot format, using EXACTLY the same
rules as the app's own refresh (src/MarketFeed.cpp :: aggregate):

  - the eight compared home valleys,
  - listings with an empty item id, a non-positive quantity or a
    non-positive price are ignored whole (they contribute nothing, not
    even their timestamp),
  - the cheapest lot per item per market, compared on the exact
    price/quantity ratio, stored rounded to four decimals,
  - the comparison table keeps items seen on two or more markets, ranked
    by total quantity, capped at 400 (ties broken by item id so every
    run is deterministic),
  - plus the recipe-ingredient closure that lets Craft & Sell cost
    recipes (capped at 800 each for outputs and ingredients).

One file per observation is written to data/<collected>.json, named for
the epoch the feed itself carries. A file is only written when the
market content actually changed:

  - same content (ignoring the collection timestamps) -> nothing written,
  - corrected content under the same timestamp -> the file is replaced.

Anything older than RETENTION_DAYS is pruned, and the manifest is
rewritten only when the file list actually changed, so an unchanged run
produces no commit at all.

A fetch or parse failure exits non-zero and touches nothing on disk.

Usage: python3 collector/collect_market.py
"""

import hashlib
import json
import os
import sys
import time
import urllib.request

INDEX_URL = "https://data-cdn.gaming.tools/paxdei/market/index.json"
ITEMS_URL = "https://data-cdn.gaming.tools/paxdei/market/items.json"

# Same spread of home valleys as the app (MarketFeed::pickedZones).
PICKS = [
    "demeter/merrie/shire", "demeter/ancien/libornes",
    "tyr/ancien/tolosa", "tyr/ancien/volvestre",
    "selene/kerys/bronyr", "selene/kerys/retz",
    "balder/inis_gallia/jura", "balder/inis_gallia/langres",
]

# Same rules as the app (MarketFeed.cpp).
MIN_MARKETS = 2
MAX_ITEMS = 400
MAX_INGREDIENTS = 800
RETENTION_DAYS = 10

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")
CRAFTING_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "crafting.json")


def get(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Valleybound-collector/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def round4(unit):
    # QString::number(unit, 'f', 4) semantics: the exact binary double,
    # printed to four decimals, read back.
    return float("%.4f" % unit)


def category_of(item_id, meta):
    url = (meta or {}).get("url", "")
    if "/buildings/" in url:
        return "Buildings"
    if "/weapons/" in url or "/armors/" in url or "wearable_" in item_id:
        return "Armor & Gear"
    if "relics" in url or "sigil" in item_id:
        return "Relics"
    if item_id.startswith("item_raw_material_"):
        return "Raw Resources"
    if item_id.startswith(("item_material_", "item_crafted_")):
        return "Crafted Materials"
    if item_id.startswith("activatable_food"):
        return "Cooked Food"
    if item_id.startswith("activatable_drink"):
        return "Brews"
    return "Other"


def aggregate(index, items_meta, zones, craft):
    """The app's aggregate(), rule for rule. Returns (bundle, canonical).

    bundle is the full snapshot; canonical is the market content with the
    collection timestamps stripped — the identity used to decide whether
    anything actually changed.
    """
    zone_order = [p for p in PICKS if p in zones]
    if not zone_order:
        return None, None

    markets = []
    presence = {}    # item_id -> markets selling it
    totals = {}      # item_id -> total quantity
    for path in zone_order:
        world, domain, zone = path.split("/")
        items = {}
        collected = 0
        for listing in zones[path]:
            item_id = listing.get("item_id") or ""
            quantity = listing.get("quantity") or 0
            price = listing.get("price")
            # The app ignores these listings whole — they do not even
            # contribute their timestamp.
            if not item_id or quantity <= 0 or price is None or price <= 0:
                continue
            last_seen = listing.get("last_seen") or 0
            if last_seen > 0:
                collected = max(collected, last_seen)
            rec = items.setdefault(item_id, {"n": 0, "t": 0, "u": -1,
                                             "l": -1, "q": -1})
            rec["n"] += 1
            rec["t"] += quantity
            unit = price / quantity
            if "best" not in rec or unit < rec["best"]:
                rec["best"] = unit
                rec["u"] = round4(unit)
                rec["l"] = int(price)
                rec["q"] = quantity
        # The app refuses an empty or undated zone outright.
        if not items or collected <= 0:
            return None, None
        markets.append({"world": world, "domain": domain, "zone": zone,
                        "collected": collected, "items": items})
        for item_id, rec in items.items():
            presence[item_id] = presence.get(item_id, 0) + 1
            totals[item_id] = totals.get(item_id, 0) + rec["t"]

    # The comparison table: items on enough markets, biggest trade first,
    # ties broken by id so the result is deterministic.
    ranked = sorted((i for i in presence if presence[i] >= MIN_MARKETS),
                    key=lambda i: (-totals[i], i))
    keep = set(ranked[:MAX_ITEMS])

    # Craft-to-sell closure, in the app's exact order: outputs first
    # (capped on the full sorted list), then ingredients not already kept.
    craftable_outputs = set()
    ingredients = set()
    for recipe in craft.get("recipes", []):
        output = recipe.get("output")
        if output in presence:
            craftable_outputs.add(output)
            for io in recipe.get("inputs", []):
                ingredients.add(io["item"])
    craftable_outputs = sorted(craftable_outputs)[:MAX_INGREDIENTS]
    priced_ingredients = sorted(
        i for i in ingredients if i not in keep and i in presence
    )[:MAX_INGREDIENTS]
    keep.update(craftable_outputs)
    keep.update(priced_ingredients)

    names = {}
    for item_id in sorted(keep):
        meta = items_meta.get(item_id)
        names[item_id] = {
            "name": (meta or {}).get("name", {}).get("En", item_id),
            "category": category_of(item_id, meta),
        }

    bundle_markets = []
    canonical_markets = []
    bundle_collected = 0
    for m in markets:
        out = {}
        for item_id in sorted(m["items"]):
            if item_id not in keep:
                continue
            rec = m["items"][item_id]
            out[item_id] = {"l": rec["l"], "q": rec["q"], "n": rec["n"],
                            "t": rec["t"], "u": rec["u"]}
        bundle_markets.append({"world": m["world"], "domain": m["domain"],
                               "zone": m["zone"], "collected": m["collected"],
                               "items": out})
        canonical_markets.append({"world": m["world"], "domain": m["domain"],
                                  "zone": m["zone"], "items": out})
        bundle_collected = max(bundle_collected, m["collected"])

    bundle = {
        "format": 1,
        "source": {
            "name": "Pax Dei community market feed (paxdei.gaming.tools)",
            "url": "https://paxdei.gaming.tools/market/api",
            "terms": ("Unofficial public feed approved by the Pax Dei developers, "
                      "refreshed hourly; free tools only. Prices are asking prices "
                      "for whole lots; per-unit values are derived."),
        },
        "collected": bundle_collected,
        "markets": bundle_markets,
        "items": names,
    }
    canonical = {"markets": canonical_markets, "items": names}
    return bundle, canonical


def canonical_hash(canonical):
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")).hexdigest()


def stored_canonical_hash(path):
    """The content hash of a snapshot already on disk, timestamps
    stripped — None when the file is missing or unreadable."""
    try:
        with open(path) as handle:
            bundle = json.load(handle)
    except (OSError, ValueError):
        return None
    try:
        canonical = {
            "markets": [{"world": m["world"], "domain": m["domain"],
                         "zone": m["zone"], "items": m["items"]}
                        for m in bundle["markets"]],
            "items": bundle["items"],
        }
    except (KeyError, TypeError):
        return None
    return canonical_hash(canonical)


def main():
    try:
        index = get(INDEX_URL)
        items_meta = get(ITEMS_URL)
        by_path = {}
        for url in index:
            at = url.find("/market/")
            if at < 0 or not url.endswith(".json"):
                continue
            by_path[url[at + 8:-5]] = url
        zones = {}
        for pick in PICKS:
            if pick in by_path:
                zones[pick] = get(by_path[pick])
        with open(CRAFTING_PATH) as handle:
            craft = json.load(handle)
    except Exception as exc:   # noqa: BLE001 - any fetch failure
        print("collector: fetch failed, nothing was changed:", exc,
              file=sys.stderr)
        return 1
    if not zones:
        print("collector: no market zones could be fetched", file=sys.stderr)
        return 1

    bundle, canonical = aggregate(index, items_meta, zones, craft)
    if bundle is None:
        print("collector: the feed's numbers could not be trusted "
              "(an undated or empty zone); nothing was changed", file=sys.stderr)
        return 1

    os.makedirs(DATA_DIR, exist_ok=True)
    now = time.time()
    changed = False
    events = []

    # One file per observation, named for the epoch the feed itself
    # carries. Written only when the market content actually changed:
    #   - the same epoch with different content is a correction — replaced;
    #   - a newer epoch with identical content is not a new observation —
    #     skipped, so the file list (and the manifest, and the commit
    #     history) only grows when the market actually moved.
    name = "%d.json" % bundle["collected"]
    path = os.path.join(DATA_DIR, name)
    body = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
    new_hash = canonical_hash(canonical)
    latest_epoch = -1
    latest_hash = None
    for entry in os.listdir(DATA_DIR):
        if not entry.endswith(".json") or entry == "manifest.json":
            continue
        try:
            epoch = int(entry[:-5])
        except ValueError:
            continue
        if epoch > latest_epoch:
            latest_epoch = epoch
            latest_hash = stored_canonical_hash(
                os.path.join(DATA_DIR, entry))
    if os.path.exists(path):
        if stored_canonical_hash(path) != new_hash:
            with open(path, "w") as handle:
                handle.write(body)
            changed = True
            events.append("corrected %s" % name)
    elif latest_hash != new_hash:
        with open(path, "w") as handle:
            handle.write(body)
        changed = True
        events.append("wrote %s" % name)
    else:
        events.append("content unchanged since %d" % latest_epoch)

    # Prune past the retention window — on unchanged runs too.
    cutoff = now - RETENTION_DAYS * 86400
    for entry in os.listdir(DATA_DIR):
        if not entry.endswith(".json") or entry == "manifest.json":
            continue
        try:
            collected = int(entry[:-5])
        except ValueError:
            continue
        if collected < cutoff:
            os.remove(os.path.join(DATA_DIR, entry))
            changed = True
            events.append("pruned %s" % entry)

    # The manifest lists every kept observation with the checksum of the
    # exact file bytes, so a reader can detect corrected content. It is
    # rewritten only when the file list changed — an unchanged run
    # commits nothing at all.
    files = []
    for entry in sorted(os.listdir(DATA_DIR)):
        if not entry.endswith(".json") or entry == "manifest.json":
            continue
        try:
            collected = int(entry[:-5])
        except ValueError:
            continue
        full = os.path.join(DATA_DIR, entry)
        with open(full, "rb") as handle:
            body_bytes = handle.read()
        try:
            parsed = json.loads(body_bytes)
            n_items = len(parsed.get("items", {}))
            n_markets = len(parsed.get("markets", []))
        except ValueError:
            continue   # never list a file that cannot be read back
        files.append({
            "collected": collected,
            "file": entry,
            "size": len(body_bytes),
            "sha256": hashlib.sha256(body_bytes).hexdigest(),
            "items": n_items,
            "markets": n_markets,
        })

    manifest = {"format": 1, "generated": int(now), "files": files}
    manifest_body = json.dumps(manifest, sort_keys=True, indent=1)
    # The manifest is rewritten only when the file list actually changed —
    # the "generated" stamp is refreshed at that moment, never on its own,
    # so an unchanged run commits nothing at all.
    old_files = None
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH) as handle:
            try:
                old_files = json.load(handle).get("files")
            except ValueError:
                old_files = None
    if changed or old_files != files:
        with open(MANIFEST_PATH, "w") as handle:
            handle.write(manifest_body)
        changed = True
        events.append("manifest updated")

    print("collector: %d file(s), %d item(s), %d market(s)%s"
          % (len(files), bundle and len(bundle["items"]),
             bundle and len(bundle["markets"]),
             (" — " + "; ".join(events)) if events else " — no change"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
