#!/usr/bin/env python3
"""Flipp API recon script — probe endpoints with US zip 03301 (Concord, NH).

Discovers which target stores have flyer data, what fields are available,
and whether the search endpoint works for US locations.
"""

import json
import logging
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scrapers.flipp_client import FlippClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

RECON_DIR = Path(__file__).resolve().parent.parent / "data" / "recon"
TARGET_STORES = {"walmart", "market basket", "aldi", "shaw", "hannaford", "bj"}
SEARCH_QUERIES = ["canned vegetables", "pasta", "diapers"]


def save_json(data, filename: str) -> None:
    """Write data to a JSON file in the recon directory."""
    RECON_DIR.mkdir(parents=True, exist_ok=True)
    path = RECON_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info("Saved %s", path)


def match_target_store(merchant_name: str) -> str | None:
    """Return the matched target store key, or None."""
    lower = merchant_name.lower()
    for target in TARGET_STORES:
        if target in lower:
            return target
    return None


def recon_flyers(client: FlippClient) -> dict:
    """Fetch flyers, save raw data, return summary info."""
    summary: dict = {
        "flyers_endpoint_responded": False,
        "total_flyers": 0,
        "top_level_keys": [],
        "merchants_found": [],
        "target_stores_found": [],
        "target_stores_missing": list(TARGET_STORES),
    }

    data = client.get_flyers()
    if data is None:
        logger.warning("Flyers endpoint returned nothing — Flipp may not support US zip codes")
        return summary

    save_json(data, "flyers_03301.json")
    summary["flyers_endpoint_responded"] = True

    if isinstance(data, dict):
        summary["top_level_keys"] = list(data.keys())
        logger.info("Top-level keys: %s", summary["top_level_keys"])
        flyers = data.get("flyers", data.get("data", []))
    elif isinstance(data, list):
        flyers = data
    else:
        flyers = []

    summary["total_flyers"] = len(flyers)
    logger.info("Total flyers returned: %d", len(flyers))

    found_targets = set()
    for flyer in flyers:
        merchant = flyer.get("merchant", flyer.get("merchant_name", "unknown"))
        if isinstance(merchant, dict):
            merchant = merchant.get("name", str(merchant))
        summary["merchants_found"].append(merchant)
        target = match_target_store(str(merchant))
        if target:
            found_targets.add(target)
            logger.info("TARGET MATCH: '%s' → %s", merchant, target)

    summary["target_stores_found"] = sorted(found_targets)
    summary["target_stores_missing"] = sorted(TARGET_STORES - found_targets)
    # deduplicate merchant list for summary
    summary["merchants_found"] = sorted(set(summary["merchants_found"]))

    logger.info("Target stores found: %s", summary["target_stores_found"])
    logger.info("Target stores missing: %s", summary["target_stores_missing"])

    return summary


def recon_items(client: FlippClient, flyers_data) -> dict:
    """Fetch items from up to 5 grocery-relevant flyers."""
    summary: dict = {"flyers_sampled": 0, "item_fields": [], "sample_items": []}

    if flyers_data is None:
        return summary

    if isinstance(flyers_data, dict):
        flyers = flyers_data.get("flyers", flyers_data.get("data", []))
    elif isinstance(flyers_data, list):
        flyers = flyers_data
    else:
        return summary

    # Prefer target-store flyers, then take first available
    scored = []
    for flyer in flyers:
        merchant = flyer.get("merchant", flyer.get("merchant_name", ""))
        if isinstance(merchant, dict):
            merchant = merchant.get("name", str(merchant))
        is_target = match_target_store(str(merchant)) is not None
        scored.append((not is_target, str(merchant), flyer))
    scored.sort(key=lambda x: (x[0], x[1]))

    sampled = 0
    for _, merchant, flyer in scored[:5]:
        flyer_id = flyer.get("id")
        if flyer_id is None:
            continue

        items = client.get_flyer_items(flyer_id)
        safe_name = "".join(c if c.isalnum() else "_" for c in merchant)
        save_json(items, f"items_{safe_name}_{flyer_id}.json")
        sampled += 1

        if items:
            first = items[0] if isinstance(items, list) else {}
            fields = list(first.keys()) if isinstance(first, dict) else []
            if fields and not summary["item_fields"]:
                summary["item_fields"] = fields
                logger.info("Item fields: %s", fields)

            sample = items[:10] if isinstance(items, list) else []
            for item in sample:
                summary["sample_items"].append({
                    "merchant": merchant,
                    "name": item.get("name", "?"),
                    "price": item.get("price", item.get("current_price", "?")),
                    "brand": item.get("brand", "?"),
                })

            logger.info("Flyer %d (%s): %d items", flyer_id, merchant, len(items))

    summary["flyers_sampled"] = sampled
    return summary


def recon_search(client: FlippClient) -> dict:
    """Test the search endpoint with target queries."""
    summary: dict = {"search_endpoint_responded": False, "queries": {}}

    for query in SEARCH_QUERIES:
        result = client.search_items(query)
        safe_q = query.replace(" ", "_")
        if result is not None:
            save_json(result, f"search_{safe_q}.json")
            summary["search_endpoint_responded"] = True

            # Try to count results
            if isinstance(result, dict):
                items = result.get("items", result.get("results", []))
            elif isinstance(result, list):
                items = result
            else:
                items = []
            summary["queries"][query] = len(items)
            logger.info("Search '%s': %d results", query, len(items))
        else:
            summary["queries"][query] = 0
            logger.warning("Search '%s': no response", query)

    return summary


def main() -> None:
    client = FlippClient(postal_code="03301")
    logger.info("Starting Flipp API recon for zip 03301 (Concord, NH)")
    logger.info("Session ID: %s", client.sid)

    # 1. Flyers
    flyers_summary = recon_flyers(client)

    # Reload raw data for item fetching
    flyers_raw = None
    flyers_file = RECON_DIR / "flyers_03301.json"
    if flyers_file.exists():
        with open(flyers_file) as f:
            flyers_raw = json.load(f)

    # 2. Items
    items_summary = recon_items(client, flyers_raw)

    # 3. Search
    search_summary = recon_search(client)

    # 4. Final summary
    summary = {
        "postal_code": "03301",
        "flyers": flyers_summary,
        "items": items_summary,
        "search": search_summary,
    }
    save_json(summary, "summary.json")

    # Print human-readable summary
    print("\n" + "=" * 60)
    print("FLIPP API RECON SUMMARY — Zip 03301 (Concord, NH)")
    print("=" * 60)

    print(f"\nFlyers endpoint responded: {flyers_summary['flyers_endpoint_responded']}")
    print(f"Total flyers found: {flyers_summary['total_flyers']}")
    print(f"Top-level response keys: {flyers_summary['top_level_keys']}")

    if flyers_summary["merchants_found"]:
        print(f"\nAll merchants ({len(flyers_summary['merchants_found'])}):")
        for m in flyers_summary["merchants_found"]:
            marker = " ← TARGET" if match_target_store(m) else ""
            print(f"  - {m}{marker}")

    print(f"\nTarget stores FOUND: {flyers_summary['target_stores_found']}")
    print(f"Target stores MISSING: {flyers_summary['target_stores_missing']}")

    if items_summary["item_fields"]:
        print(f"\nItem fields available: {items_summary['item_fields']}")
    print(f"Flyers sampled for items: {items_summary['flyers_sampled']}")
    if items_summary["sample_items"]:
        print("\nSample items:")
        for item in items_summary["sample_items"][:5]:
            print(f"  - [{item['merchant']}] {item['name']} — {item['price']}")

    print(f"\nSearch endpoint responded: {search_summary['search_endpoint_responded']}")
    for q, count in search_summary["queries"].items():
        print(f"  '{q}': {count} results")

    print("\n" + "=" * 60)
    print(f"Raw data saved to: {RECON_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
