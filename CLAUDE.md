# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run Commands

This is a Python 3.11+ project. No code has been written yet — the file structure below is the planned layout.

```bash
# Install dependencies (once pyproject.toml exists)
pip install -e ".[dev]"

# Run scrapers
python scripts/run_scrapers.py

# Generate weekly report
python scripts/weekly_report.py

# Run tests
pytest
pytest tests/test_aldi.py           # single file
pytest tests/test_aldi.py::test_fn  # single test
```

## Project Overview

A free, open-source tool that compares grocery prices across stores in New Hampshire, focused on staple categories that matter most to food pantries, mutual aid organizations, and budget-conscious families. Born from a real community need — a local farm (Restoration Acres Farm) that cooks meals for people living alone couldn't receive food bank support and needed help finding the cheapest sources for bulk staples.

**Primary users:** Food pantries, mutual aid orgs, community kitchens, families on tight budgets in NH.

**Core question the tool answers:** "For a set of staple grocery categories, where in NH should I buy to spend the least money this week?"

## Target Categories

These are the categories identified from the original community request:

- Canned vegetables
- Canned soups
- Bulk pasta
- Bulk rice
- Bulk oats
- Bulk cereal
- Bulk seasoning/spices
- Frozen vegetables
- Frozen small meals (pot pies, etc.)
- Paper goods (toilet paper)
- Paper towels
- Diapers

## Target Stores (Concord NH Area)

### Tier 1 — Via Flipp API (weekly sale/flyer prices)
- **Walmart** — prices match in-store
- **Market Basket** — consistently cheapest everyday grocer in New England
- **Aldi** — discount grocer, store-brand focused
- **Shaw's** — Albertsons-owned, major NH chain
- **Hannaford** — major NH chain, 189 stores in Northeast
- **BJ's Wholesale** — bulk/warehouse club

### Tier 2 — Direct catalog scraping (everyday/baseline prices)
- **Aldi** (aldi.us) — CONFIRMED: product catalog pages serve structured price data in plain HTML. No JS rendering needed. Clean category URLs. This is the most reliable free data source.
  - Example: `https://www.aldi.us/products/pantry-essentials/canned-foods/k/102`
  - Data available: product name, brand, size/weight, price
  - Paginated (page param in URL)
  - Relevant category URLs:
    - Canned Foods: `/products/pantry-essentials/canned-foods/k/102`
    - Soups & Broth: `/products/pantry-essentials/soups-broth/k/105`
    - Pasta, Rice & Grains: `/products/pantry-essentials/pasta-rice-grains/k/108`
    - Spices: `/products/pantry-essentials/spices/k/106`
    - Frozen Vegetables: `/products/frozen-foods/frozen-vegetables/k/163`
    - Frozen Meals & Sides: `/products/frozen-foods/frozen-meals-sides/k/137`
    - Paper & Plastic Products: `/products/household-essentials/paper-plastic-products/k/164`
    - Cereal & Oatmeal: `/products/breakfast-cereals/cereal-oatmeal/k/162`
    - Diapers, Wipes & Wash: `/products/baby-items/diapers-wipes-wash/k/53`

### Tier 3 — Paid APIs (future, only if tool gains traction)
- **SerpAPI** ($75/mo for 5,000 searches) or **SearchAPI** ($40/mo) for Walmart product data
- Only needed if free scraping paths prove insufficient

## Architecture

### Data Pipeline

```
┌─────────────────────────────────────────────────────┐
│                   Data Sources                       │
├──────────────┬──────────────┬───────────────────────┤
│  Flipp API   │  Aldi.us     │  Future: Paid APIs    │
│  (flyer/sale │  (everyday   │  (Walmart, etc.)      │
│   prices)    │   catalog)   │                       │
└──────┬───────┴──────┬───────┴───────────┬───────────┘
       │              │                   │
       ▼              ▼                   ▼
┌─────────────────────────────────────────────────────┐
│              Scrapers / Collectors                    │
│  - flipp_scraper.py (adapted from flippscrape)      │
│  - aldi_scraper.py (HTTP + HTML parse)              │
│  - future: walmart_scraper.py                       │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              Data Normalization                       │
│  - Normalize product names                           │
│  - Compute unit prices (price per oz, per count)     │
│  - Categorize into target categories                 │
│  - Tag as "sale" vs "everyday" price                 │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              Storage (SQLite or JSON)                 │
│  - products table (name, store, price, unit_price,   │
│    category, price_type, valid_from, valid_to,       │
│    scraped_at)                                       │
│  - price_history table (for trend tracking)          │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              Output / Presentation                   │
│  Phase 1: CLI + CSV/JSON reports                     │
│  Phase 2: Simple static site (Hugo or plain HTML)    │
│  Phase 3: Signal notifications via signal-cli        │
└─────────────────────────────────────────────────────┘
```

### Tech Stack

- **Language:** Python 3.11+
- **HTTP:** `requests` (no headless browser needed for Phase 1)
- **HTML Parsing:** `beautifulsoup4` + `lxml`
- **Data:** `pandas` for manipulation, SQLite for storage
- **Output:** CSV/JSON initially, potentially Hugo static site later
- **Scheduling:** cron job or systemd timer for weekly runs
- **Notifications (future):** signal-cli-rest-api (already running on home lab)

## Development Phases

### Phase 1: Data Collection & Proof of Concept
1. **Flipp API recon** — Hit the Flipp API with zip 03301 (Concord NH), discover which stores return data, understand the response schema. Adapt the approach from [flippscrape](https://github.com/Kiizon/flippscrape) for US zip codes.
2. **Aldi catalog scraper** — Scrape aldi.us product pages for everyday baseline prices across all target categories. Parse product name, brand, size, price from HTML.
3. **Data normalization** — Map scraped products into the target categories. Compute unit prices where possible.
4. **Comparison report** — Generate a simple "best prices this week by category" output as CSV and/or markdown table.

### Phase 2: Expand & Automate
5. **Store coverage** — Based on Flipp results, add any stores that need direct scraping (Shaw's, Hannaford have online grocery but are JS-rendered — may need to reverse-engineer their APIs or use a headless browser).
6. **Weekly automation** — Set up cron to run scrapers weekly, store results in SQLite, track price history.
7. **Signal alerts** — Use existing signal-cli-rest-api to send weekly "best deals" summary to a Signal group.

### Phase 3: Community Tool
8. **Static website** — Simple site showing current best prices by category, updated weekly. Could host on Cloudflare Pages alongside oscargala.com.
9. **Category-based shopping lists** — "If you need to buy canned goods, frozen meals, and diapers this week, here's your optimal store-by-store list."
10. **Open source & outreach** — MIT license, share with NH mutual aid network, Restoration Acres, and other community organizations.

## Key Design Decisions

- **Categories over SKUs:** The tool compares at the category level ("cheapest canned green beans anywhere") not the SKU level ("this specific UPC at Store A vs Store B"). Food pantries buy whatever is cheapest, not brand-loyal.
- **Unit price is king:** A 15oz can for $0.85 vs a 28oz can for $1.65 — the unit price ($/oz) is what matters for bulk purchasing decisions.
- **Sale vs everyday:** Clearly distinguish flyer/sale prices (temporary) from everyday catalog prices (stable). Both are valuable.
- **Free-first:** No paid APIs until the tool proves useful. Aldi catalog + Flipp flyer data should cover the MVP.
- **Privacy-respecting:** No user accounts, no tracking. Static output that anyone can access.

## Flipp API Notes

Based on the existing [flippscrape](https://github.com/Kiizon/flippscrape) project (targets Canadian stores):
- The scraper generates a session ID, fetches flyers by postal/zip code, filters for grocery stores, and extracts item-level deal data.
- Output fields: merchant, flyer_id, name, price, valid_from, valid_to
- Flipp gets its data directly from retailers (not scraping) — retailers pay Flipp to distribute their flyer content.
- The API is location-based — passing a US zip code should return US store flyers.
- **First task:** Reverse-engineer or adapt the flippscrape approach for zip 03301 and document which stores/endpoints work.

## Aldi Scraping Notes

Confirmed working approach:
- Standard HTTP GET to category pages returns full product data in HTML
- No authentication, no JS rendering required
- Products listed with: brand name, product name, size/weight, price
- Pages are paginated (`?page=2`)
- Rate limiting: be respectful, add delays between requests
- Sample data point from recon: Happy Harvest Cut Green Beans 14.5 oz = $0.85

## File Structure

```
nh-grocery-prices/
├── CLAUDE.md              # This file
├── README.md              # Public-facing project description
├── LICENSE                # MIT
├── pyproject.toml         # Project config (or requirements.txt)
├── src/
│   ├── scrapers/
│   │   ├── __init__.py
│   │   ├── flipp.py       # Flipp API scraper
│   │   ├── aldi.py        # Aldi catalog scraper
│   │   └── base.py        # Base scraper class/interface
│   ├── normalize.py       # Product name normalization, categorization
│   ├── compare.py         # Cross-store comparison logic
│   ├── storage.py         # SQLite read/write
│   └── report.py          # Output generation (CSV, markdown, JSON)
├── data/
│   ├── categories.json    # Category definitions and keyword mappings
│   └── stores.json        # Store metadata (name, type, zip, data source)
├── output/                # Generated reports
├── tests/
│   ├── test_flipp.py
│   ├── test_aldi.py
│   └── test_normalize.py
└── scripts/
    ├── run_scrapers.py    # Main entry point
    └── weekly_report.py   # Generate weekly comparison
```

## Coding Conventions

- Python 3.11+ with type hints
- Use `dataclasses` or `pydantic` for data models
- `logging` module for output (not print statements)
- Respectful scraping: delays between requests, proper User-Agent, don't hammer endpoints
- All scrapers should implement a common interface so adding new stores is straightforward
- Tests for parsing logic (use saved HTML fixtures, not live requests)
- Keep secrets out of code (API keys go in env vars or .env file)

## Prior Art / References

- [flippscrape](https://github.com/Kiizon/flippscrape) — Python Flipp scraper (Canadian stores, adaptable)
- [flipp_flyer_parser](https://github.com/FriendlyUser/flipp_flyer_parser) — More elaborate Flipp parser with Selenium
- [Flipp corporate](https://corp.flipp.com/) — Flipp's B2B platform info
- [Aldi US products](https://www.aldi.us/products) — Aldi's browsable catalog
- Market Basket digital flyer: https://www.shopmarketbasket.com/weekly-flyer/
- Shaw's weekly ad: https://www.shaws.com/weeklyad/
- Hannaford: https://hannaford.com/weekly-flyer

## Portfolio / Storytelling Context

This project fits into a broader civic tech portfolio alongside CommentGuard (fake federal comment detection). The narrative: using technology to serve communities — in this case, helping food pantries and families navigate rising grocery costs. The "why" matters more than the "how" for public presentation. The origin story (Restoration Acres Farm post, wife's suggestion that AI could help) is authentic and compelling.

Potential blog post angle: "My wife saw a Facebook post and wondered if AI could help. Here's what we built."
