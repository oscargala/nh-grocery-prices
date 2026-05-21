# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run Commands

Python 3.11+. No `pyproject.toml` yet — the project runs from source against system-installed deps.

```bash
# Run the full pipeline (Flipp + Firecrawl across 7 stores)
python3 scripts/run_scrapers.py

# Re-render the GitHub Pages dashboard from cached data (no scraping)
python3 scripts/export_static.py

# Force re-scrape (ignore cache freshness)
python3 scripts/run_scrapers.py --staleness-days 0

# Only scrape a subset of categories (useful for testing)
python3 scripts/run_scrapers.py --categories canned_vegetables pasta rice
```

A run takes ~5–8 minutes end-to-end (Flipp is fast; Firecrawl is rate-limited to ~10 RPM).
The pipeline reads `FIRECRAWL_API_KEY` from `.env`. Cached Firecrawl results live in
`data/cache/*.json` and are reused across runs until `staleness_days` expires (default 7).

## Project Overview

A free, open-source tool that compares grocery prices across stores in New Hampshire, focused on staple categories that matter most to food pantries, mutual aid organizations, and budget-conscious families. Born from a real community need — a local farm (Restoration Acres Farm) that cooks meals for people living alone couldn't receive food bank support and needed help finding the cheapest sources for bulk staples.

**Primary users:** Food pantries, mutual aid orgs, community kitchens, families on tight budgets in NH.

**Core question the tool answers:** "For a set of staple grocery categories, where in NH should I buy to spend the least money this week?"

## Current State (as of 2026-05-21)

The pipeline is **working end-to-end** and deploying to GitHub Pages. Latest full-run numbers:

- **1,299 categorized products** across **36 categories** and **7 stores**
- Dashboard live at https://oscargala.github.io/nh-grocery-prices/
- ~1.3 MB raw / ~91 KB gzipped static HTML

Source mix:
- **Flipp flyer API:** 875 sale-price products from 7 stores (Walmart, Aldi, Shaw's, Hannaford, Market Basket, Sam's Club, Costco — and BJ's when their flyer publishes)
- **Firecrawl catalog:** 424 everyday-price products from 3 stores (Aldi 63, Walmart 305, Sam's Club 182)

## Target Categories (36 total)

Original 12 staples (from the Restoration Acres community request):
Canned Vegetables, Canned Soups, Pasta, Rice, Oats & Oatmeal, Cereal, Spices & Seasoning,
Frozen Vegetables, Frozen Meals, Toilet Paper, Paper Towels, Diapers.

Added in May 2026 (driven by Flipp data we were dropping — see "Trade-offs" below):
Milk, Eggs, Cheese, Butter & Margarine, Yogurt, Ice Cream & Frozen Desserts,
Coffee & Tea, Soda & Soft Drinks, Juice, Bottled Water, Bread & Bakery, Snacks,
Cookies & Candy, Laundry, Chicken, Beef, Pork, Seafood, Deli Meat & Bacon,
Pasta & Pizza Sauce, Condiments & Dressings, Peanut Butter & Jam, Baking Essentials,
Fresh Produce.

Definitions and keyword regexes live in `data/categories.json`. Categorization runs at
collection time and again on cache reload (so `categories.json` edits apply to cached
products without re-scraping).

## Target Stores (Concord NH Area)

### Tier 1 — Via Flipp API (weekly sale/flyer prices) — WORKING
All 7 stores below return data via Flipp for zip 03301:
- **Walmart** — prices match in-store; flyer often non-grocery (lawn, electronics)
- **Market Basket** — consistently cheapest everyday grocer in New England; 2 parallel flyers
- **Aldi** — discount grocer, store-brand focused; runs 2 flyers (the "Aldi Finds" non-grocery flyer + the actual weekly grocery flyer — see "Multi-flyer fix" below)
- **Shaw's** — Albertsons-owned, major NH chain; **3 flyers** (Big Book of Savings + 2 Weekly Ads — alone went 26 → 330 categorized once we fetched all three)
- **Hannaford** — major NH chain, 189 stores in Northeast; 1 flyer
- **Sam's Club** — bulk/warehouse club
- **Costco** — bulk/warehouse club; 2 flyers (CP Grocery + general)
- **BJ's Wholesale** — listed in `stores.json` but typically doesn't surface a flyer for this zip

### Tier 2 — Catalog scraping via Firecrawl (everyday/baseline prices) — WORKING
Aldi migrated `aldi.us` to an Instacart-powered SPA (sometime between Mar–May 2026); plain-HTML
scraping returns nothing. Walmart's PerimeterX/Akamai stack made the local Camoufox stealth
scraper unreliable (captcha rate ~92%). Sam's Club is the same anti-bot stack.

All three now go through **cloud Firecrawl** (`https://api.firecrawl.dev/v1/scrape`) with
AI JSON-schema extraction. URL configs live in `src/scrapers/firecrawl_store.py`:

- **Aldi** — 12 parent-category URLs under `/products/...`. Aldi's parent pages render only
  a carousel preview, so a `follow_links_pattern` (regex `/store/aldi/collections/rc-[a-z0-9-]+`)
  discovers sub-shelf URLs from each parent page and scrapes those too. Caps at 30 follows
  per run to stay under the rate limit.
- **Walmart** — search URLs `/search?q=...&store_id=2055` (Concord NH). **3 pages per query**
  (took Walmart 172 → 305 products, ~57% gain).
- **Sam's Club** — search URLs `/s/...?clubId=6604` (Concord NH). **3 pages per query**.

Free tier is 10 RPM; the scraper paces ~6.5s between requests. ~80–100 calls per full run
(was 32 before pagination + Aldi follow). Weekly cadence stays under the 500-credit free
limit; if we move to daily that becomes ~2,500/month and we'd need the $16/mo Hobby tier.

### Tier 3 — Skipped for now (everyday catalog unavailable)
- **Hannaford** — Datadome anti-bot blocks Firecrawl proxies; only Flipp flyer data.
- **Shaw's** — Albertsons SPA with login/store-selector gate; only Flipp flyer data.
- **Market Basket** — no public catalog (their site is a flyer viewer; `/products/...` 404s); only Flipp flyer data.
- **Costco** — landing-only browsing without member context; only Flipp flyer data.
- **BJ's Wholesale** — Firecrawl can scrape it cleanly but needs a `$1599 → $15.99`
  price-format fix and ~12 category URLs. **Deferred** — not worth the integration cost
  for one more store right now; revisit if Flipp coverage for BJ's stays thin.

## Trade-offs Made

These shape why the codebase looks the way it does. Listed in roughly the order we hit them.

### Scrape strategy

- **Categories over SKUs.** We compare at the category level ("cheapest canned green beans
  anywhere"), not by UPC. Food pantries and budget shoppers buy whatever is cheapest, not
  brand-loyal. This shifts complexity from data joins to good category regexes in
  `data/categories.json`.

- **Sale vs. everyday prices, both kept.** Flipp = temporary sale prices. Firecrawl
  catalog = stable everyday prices. Each product carries `price_type` so the dashboard
  can show both honestly without conflating them.

- **Free-tier-first on Firecrawl.** Cloud Firecrawl free tier (500 credits/month) covers
  our weekly cadence with headroom. Self-hosted Firecrawl works for Aldi (JS rendering)
  but **not** for Walmart/Sam's anti-bot — cloud Firecrawl's residential proxies are the
  only thing currently bypassing PerimeterX reliably. The realistic upgrade path if we
  outgrow the free tier is the $16/mo Hobby plan, not self-hosting.

- **Local stealth scrapers (Camoufox) deprecated.** `src/scrapers/walmart_stealth.py`,
  `samsclub_stealth.py`, and the `playwright/` subpackage are kept in-tree as reference
  but **not wired into the pipeline**. Walmart captcha rate was ~92%. Firecrawl replaced
  the whole stack.

### Data collection

- **Use every Flipp flyer per store.** Aldi, Shaw's, Market Basket, and Costco run
  multiple parallel weekly flyers (e.g., Aldi has an "In Store Ad" and a "Weekly Ad").
  Original code grabbed the first one returned, which for Aldi was the "Aldi Finds"
  non-grocery flyer (0 categorized items). Now we iterate all flyers and dedupe by name.
  Shaw's alone jumped 26 → 330 categorized after this fix.

- **3-page pagination for Walmart/Sam's.** Firecrawl returns ~20 products per search page
  by default. Scraping pages 1–3 per category triples Walmart coverage at the cost of
  ~24 more API calls per run. Failures on page 2 or 3 are tolerated — the pipeline keeps
  going.

- **Aldi sub-shelf follow.** Aldi parent category pages render only a carousel preview
  (~18 products). The `rc-*` sub-shelf URLs link to full category views (~27+ products
  each). The scraper's `follow_links_pattern` regex discovers these dynamically and
  scrapes them in a second pass.

- **Drop ~94% of Flipp items by design (and recover them via more categories).** Flipp
  flyers contain everything from canned beans to outdoor furniture. We only keep items
  matching one of our 36 category regexes. Before May 2026 we had 12 categories and were
  dropping 1,012 of 1,075 priced Flipp items; expanding to 36 categories took us from
  63 categorized to ~875. The remaining drops are real non-grocery items.

- **Cache hot reload.** Cached Firecrawl products live in `data/cache/*.json` keyed by
  store + category. When `categories.json` changes, `_load_cache()` re-categorizes cached
  products on read, so regex tweaks apply without burning new API credits. Includes a
  fallback: if a re-categorize returns `None` but the cached `category` is still a known
  ID, keep the product (prevents losing products to overly-strict regex edits).

### Output

- **Static HTML on GitHub Pages, no backend.** Dashboard is a single `docs/index.html`
  rebuilt by `scripts/export_static.py`. GitHub Pages serves `/docs` from the `headless`
  branch. No JS framework, no API server, no database — just one file with all categories
  collapsible client-side. ~1.3 MB / 91 KB gzipped. Privacy-respecting (no tracking,
  no user accounts), zero hosting cost.

- **Per-category collapse + jump-nav.** Each category renders only its 10 cheapest by
  default; "Show all N" reveals the rest. A jump-nav row at the top lets visitors hop
  directly to a category. Keeps the page usable even with 1,000+ products.

## Architecture

### Data Pipeline

```
┌─────────────────────────────────────────────────────┐
│                   Data Sources                       │
├──────────────┬──────────────────────────────────────┤
│  Flipp API   │  Firecrawl Cloud (JS render + proxy) │
│  (7 stores,  │  → Aldi, Walmart, Sam's Club         │
│   sale)      │     (everyday catalog)               │
└──────┬───────┴──────────────────┬───────────────────┘
       │                          │
       ▼                          ▼
┌─────────────────────────────────────────────────────┐
│              Scrapers / Collectors                    │
│  - src/scrapers/flipp.py   (Flipp HTTP, all flyers) │
│  - src/scrapers/firecrawl_store.py                  │
│    (Aldi + Walmart + Sam's via Firecrawl extract,   │
│     w/ pagination + sub-shelf follow)               │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              Normalization & Categorization           │
│  - src/normalize.py                                  │
│  - 36 regex-based categories in data/categories.json │
│  - Unit price computed where size parses             │
│  - price_type tagged as "sale" or "everyday"         │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              Storage                                  │
│  - data/cache/{aldi,walmart,sams_club}.json          │
│    (Firecrawl results, keyed by category)            │
│  - output/products_YYYY-MM-DD.csv                    │
│    (full categorized snapshot per run)               │
│  (No SQLite yet — JSON files have been enough)       │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              Output                                   │
│  - docs/index.html (static, GitHub Pages)            │
│  - CSV (output/)                                     │
│  - Console summary (run_scrapers.py)                 │
│  Future: Signal notifications via signal-cli         │
└─────────────────────────────────────────────────────┘
```

### Tech Stack

- **Language:** Python 3.11+, type hints
- **HTTP:** `requests`
- **Scraping:** Firecrawl Cloud for JS/anti-bot; Flipp's own HTTP API for flyers
- **Data:** plain JSON files in `data/cache/`; CSV in `output/`
- **Output:** static HTML via Jinja templates in `templates/`
- **Deployment:** GitHub Pages serves `/docs` on the `headless` branch
- **Scheduling:** manual for now; weekly cron is the next operational step
- **Notifications (future):** signal-cli-rest-api (already running on home lab)

## Roadmap

### Done
- Flipp ingestion for 7 stores (all flyers per store, deduped)
- Firecrawl-backed everyday catalog for Aldi / Walmart / Sam's Club
- 36-category taxonomy with regex matching and unit-price computation
- Static HTML dashboard with per-category collapse + jump-nav
- GitHub Pages deployment from `headless` branch
- CSV snapshot per run in `output/`
- Cache with hot reload on `categories.json` edits

### Next operational steps
- **Weekly cron.** Wire `python3 scripts/run_scrapers.py && python3 scripts/export_static.py && git push` into a systemd timer or cron job. The data is timestamped and the dashboard is idempotent.
- **Price history.** Today each run overwrites the previous CSV/cache. Stash old `output/products_*.csv` (or move to SQLite) for trend tracking.
- **Signal alerts.** Send a "best deals this week" digest via the existing signal-cli-rest-api on the home lab.

### Future expansion (deferred)
- **BJ's Wholesale catalog** via Firecrawl — verified scrapable but needs `$1599 → $15.99` price-format handling and ~12 category URLs.
- **More stores** would require either (a) Firecrawl + a paid premium anti-bot service like Bright Data Unlocker for Hannaford/Shaw's, or (b) acceptance that Flipp coverage is enough for those four.
- **Daily cadence.** Would push Firecrawl usage to ~2,500 calls/month — needs the $16/mo Hobby tier.

## File Structure

```
shopping/
├── CLAUDE.md                       # This file
├── README.md                       # Public-facing project description
├── .env                            # FIRECRAWL_API_KEY (gitignored)
├── data/
│   ├── categories.json             # 36 categories with keyword regexes
│   ├── stores.json                 # Store metadata (id, aliases, tier)
│   ├── cache/                      # Firecrawl per-store caches (gitignored)
│   └── recon/                      # Saved API responses for testing (gitignored)
├── docs/
│   └── index.html                  # GitHub Pages dashboard (committed)
├── output/                         # CSV snapshots per run (gitignored)
├── scripts/
│   ├── run_scrapers.py             # Pipeline entry point
│   ├── export_static.py            # Render docs/index.html from cache + last run
│   └── flipp_recon.py              # One-off Flipp endpoint exploration
├── src/
│   ├── models.py                   # Dataclasses (FlyerProduct, CategoryDef, etc.)
│   ├── normalize.py                # Categorize, parse sizes, compute unit prices
│   ├── compare.py                  # best_prices_by_category, category_summary
│   ├── web.py                      # (Stub for future web UI)
│   └── scrapers/
│       ├── flipp.py                # Flipp HTTP client + multi-flyer collection
│       ├── flipp_client.py         # Low-level Flipp API
│       ├── firecrawl_store.py      # Aldi/Walmart/Sam's via Firecrawl (active)
│       ├── aldi.py                 # Legacy HTML scraper (pre-Instacart migration)
│       ├── walmart_stealth.py      # Legacy Camoufox scraper (unused)
│       ├── walmart_api.py          # Legacy direct-API experiments (unused)
│       ├── samsclub_stealth.py     # Legacy Camoufox scraper (unused)
│       └── playwright/             # Legacy Playwright base + Shaw's/Hannaford (unused)
└── templates/
    └── dashboard.html              # Jinja template for docs/index.html
```

The `legacy` scrapers are kept in-tree for reference (price-format heuristics, anti-bot
notes) but are not imported by the pipeline. Safe to delete if disk-space matters.

## Coding Conventions

- Python 3.11+ with type hints; `dataclasses` (not pydantic) for models in `src/models.py`
- `logging` module for output (not print statements)
- Respectful scraping: delays between requests (~6.5s for Firecrawl, ~1s for Flipp), real User-Agent
- Keep secrets in `.env`, never in code (the loader in `scripts/run_scrapers.py` reads `.env` directly so no `python-dotenv` dep is needed)
- Per-store scrapers can have their own shape, but they all yield `FlyerProduct` instances back to the pipeline
- No test suite yet — categorization regex changes are validated by re-running the pipeline against cached data and eyeballing dashboard counts

## Flipp API Notes

The Flipp Backflipp API is location-based and returns retailer-pushed flyer content
(Flipp doesn't scrape — retailers pay them to distribute). Hitting it with zip 03301
returns flyers for Walmart, Aldi, Shaw's, Hannaford, Market Basket, Sam's Club, Costco,
and occasionally BJ's.

Each merchant can publish multiple parallel flyers. Always iterate them all and dedupe;
the first-returned flyer is often the wrong one (Aldi's "Aldi Finds" non-grocery flyer
beat the actual grocery flyer in the response order).

Output fields per item: merchant, flyer_id, name, price (sometimes a string like
`"$2.99"`, sometimes a number), valid_from, valid_to.

Original reference: [flippscrape](https://github.com/Kiizon/flippscrape) (Canadian stores).

## Prior Art / References

- [flippscrape](https://github.com/Kiizon/flippscrape) — Python Flipp scraper (Canadian stores, adaptable)
- [flipp_flyer_parser](https://github.com/FriendlyUser/flipp_flyer_parser) — More elaborate Flipp parser with Selenium
- [Flipp corporate](https://corp.flipp.com/) — Flipp's B2B platform info
- [Firecrawl docs](https://docs.firecrawl.dev/) — Hosted JS-render + anti-bot scrape API
- [Aldi US products](https://www.aldi.us/products) — Aldi's browsable catalog (Instacart-powered SPA)
- Market Basket digital flyer: https://www.shopmarketbasket.com/weekly-flyer/
- Shaw's weekly ad: https://www.shaws.com/weeklyad/
- Hannaford: https://hannaford.com/weekly-flyer

## Portfolio / Storytelling Context

This project fits into a broader civic tech portfolio alongside CommentGuard (fake federal comment detection). The narrative: using technology to serve communities — in this case, helping food pantries and families navigate rising grocery costs. The "why" matters more than the "how" for public presentation. The origin story (Restoration Acres Farm post, wife's suggestion that AI could help) is authentic and compelling.

Potential blog post angle: "My wife saw a Facebook post and wondered if AI could help. Here's what we built."
