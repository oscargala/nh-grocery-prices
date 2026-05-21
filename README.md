# NH Grocery Prices

A free, open-source tool that compares grocery prices across stores in New Hampshire,
focused on staple categories that matter most to food pantries, mutual aid organizations,
and budget-conscious families.

**Live dashboard:** https://oscargala.github.io/nh-grocery-prices/

## Why this exists

A local farm in southern New Hampshire — Restoration Acres Farm — cooks meals for people
living alone but doesn't qualify for food-bank support. They posted asking for help
finding the cheapest sources for bulk staples. My wife saw the post and wondered if AI
could help. So we built this.

The tool answers one question: **"For a basket of staple grocery categories, which store
in NH has the lowest prices this week?"**

It compares at the category level — "cheapest canned green beans anywhere" — rather than
matching exact UPCs. Pantries and budget shoppers buy whatever is cheapest, not by brand.

## What it covers

**36 grocery categories**, including:

- Pantry staples: canned vegetables, canned soups, pasta, rice, oats, cereal, spices
- Dairy: milk, eggs, cheese, butter, yogurt
- Proteins: chicken, beef, pork, seafood, deli meat
- Frozen: vegetables, meals, ice cream
- Drinks: coffee, tea, soda, juice, bottled water
- Household: toilet paper, paper towels, diapers, laundry
- Bread, snacks, candy, condiments, baking essentials, fresh produce, and more

**7 stores** in the Concord NH area (zip 03301):

| Store | Sale prices (Flipp) | Everyday catalog (Firecrawl) |
|---|---|---|
| Walmart | ✅ | ✅ |
| Aldi | ✅ | ✅ |
| Sam's Club | ✅ | ✅ |
| Market Basket | ✅ | — (no public catalog) |
| Shaw's | ✅ | — (Albertsons SPA gated) |
| Hannaford | ✅ | — (Datadome blocks scraping) |
| Costco | ✅ | — (member-gated) |

A typical run pulls **~1,300 categorized products** across all stores and categories.

## How it works

```
   Flipp flyer API (7 stores)        Firecrawl Cloud (Aldi, Walmart, Sam's)
            │                                       │
            └───────────────┬───────────────────────┘
                            ▼
                  Normalize & categorize
                  (36 regex-based categories,
                   unit price computation,
                   sale-vs-everyday tagging)
                            │
                            ▼
                  Compare across stores
                  (cheapest per category)
                            │
                            ▼
              ┌─────────────┴─────────────┐
              ▼                           ▼
     docs/index.html              output/products_*.csv
     (GitHub Pages)               (full snapshot)
```

**Two data sources, by design:**

- **Flipp** distributes retailers' weekly flyers (retailers pay them — Flipp doesn't
  scrape). One free API call per flyer; gives us 7 stores at zero cost. The trade-off:
  flyer items are sale-priced and temporary.
- **Firecrawl** renders JS-heavy storefronts and bypasses anti-bot proxies. We use it
  for Aldi's Instacart SPA and Walmart/Sam's PerimeterX-protected search. The trade-off:
  rate-limited (10 RPM on the free tier) and only practical for ~3 stores at our volume.

## Run it yourself

Requires Python 3.11+ and a free [Firecrawl](https://firecrawl.dev/) API key.

```bash
# Clone
git clone https://github.com/oscargala/nh-grocery-prices.git
cd nh-grocery-prices

# Set your Firecrawl key (free tier is fine — we use ~100 credits/week)
echo "FIRECRAWL_API_KEY=fc-..." > .env

# Install deps (no pyproject.toml yet — just the basics)
pip install requests jinja2

# Run the full pipeline
python3 scripts/run_scrapers.py

# Render the static dashboard
python3 scripts/export_static.py
open docs/index.html
```

### Useful flags

```bash
# Force re-scrape (ignore the 7-day cache)
python3 scripts/run_scrapers.py --staleness-days 0

# Only scrape a subset of categories
python3 scripts/run_scrapers.py --categories pasta rice cereal

# Different zip code
python3 scripts/run_scrapers.py --zip 03104  # Manchester NH
```

## Project structure

```
shopping/
├── scripts/
│   ├── run_scrapers.py        # Pipeline entry point
│   └── export_static.py       # Build docs/index.html
├── src/
│   ├── scrapers/
│   │   ├── flipp.py           # Flipp flyer ingestion (7 stores)
│   │   └── firecrawl_store.py # Aldi/Walmart/Sam's via Firecrawl
│   ├── normalize.py           # Category matching, unit-price parsing
│   ├── compare.py             # Cross-store cheapest-by-category
│   └── models.py              # Dataclasses
├── data/
│   ├── categories.json        # 36 categories with keyword regexes
│   └── stores.json            # Store metadata
├── docs/index.html            # GitHub Pages dashboard
└── templates/dashboard.html   # Jinja template for the dashboard
```

## Trade-offs

This project tries to stay free and lightweight. A few decisions worth flagging:

- **No SKU matching.** We compare by category, not exact product. A 14 oz can of generic
  green beans and a 15 oz can of name-brand are both "Canned Vegetables". This makes the
  comparison fuzzier but matches how budget shoppers actually buy.

- **Sale and everyday prices both shown.** Flipp flyer items are temporary, often
  loss-leaders. Firecrawl catalog prices are stable but only available for 3 stores. Each
  product is tagged with its source so you can tell them apart.

- **Some stores are flyer-only.** Hannaford and Shaw's have anti-bot protection that
  Firecrawl can't currently bypass; Market Basket and Costco don't expose a public
  catalog at all. For these four, we only see what's in this week's flyer.

- **BJ's Wholesale is deferred.** Firecrawl can scrape it, but the price format is
  unusual (`$1599` means `$15.99`) and would need custom parsing. Skipped for now —
  revisit if Flipp's BJ's coverage stays thin.

- **No cron yet.** Pipeline runs are still manual. The data and dashboard are
  reproducible end-to-end, so weekly automation is a small operational step away.

## Privacy

No user accounts, no tracking, no analytics. The dashboard is a static HTML file served
by GitHub Pages. If you have questions about specific items, deals, or stores covered,
file an issue.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome, especially:

- **More categories.** If a staple isn't represented, add a regex set to
  `data/categories.json` and re-run the pipeline.
- **Other NH zip codes.** The pipeline accepts `--zip` but the hardcoded Walmart and
  Sam's Club store IDs in `src/scrapers/firecrawl_store.py` (`store_id=2055`,
  `clubId=6604`) are Concord-specific. Pull requests welcome.
- **BJ's price-format handling.** See "Trade-offs" above.
- **Cron / Signal alerts.** Would be a nice operational addition.

## Acknowledgements

- Restoration Acres Farm, whose community ask started this.
- The [flippscrape](https://github.com/Kiizon/flippscrape) project for documenting the Flipp API approach for Canadian stores.
- [Firecrawl](https://firecrawl.dev/) for making cloud-side rendering + anti-bot proxying available on a generous free tier.
