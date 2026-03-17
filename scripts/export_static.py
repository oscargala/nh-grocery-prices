#!/usr/bin/env python3
"""Export the dashboard as a static HTML file for GitHub Pages."""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask
from src.web import app, _run_and_cache, _get_data
from src.normalize import load_categories

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"


def export():
    """Run the pipeline and render the dashboard to a static HTML file."""
    # Run the pipeline to populate cached data
    logger.info("Running pipeline for static export...")
    _run_and_cache()

    # Render the dashboard template with Flask's test client
    with app.test_client() as client:
        response = client.get("/")
        html = response.data.decode("utf-8")

    # Remove the refresh button and its JS (not functional in static mode)
    html = html.replace(
        '<button class="btn-refresh" id="refreshBtn" onclick="refreshData()">Refresh Data</button>',
        '<span style="color: var(--text-muted); font-size: 0.9rem;">Static snapshot — updated by automated pipeline</span>',
    )
    html = html.replace(
        '<div class="refresh-status" id="refreshStatus"></div>',
        "",
    )

    # Write to docs/ for GitHub Pages
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    index_path = DOCS_DIR / "index.html"
    index_path.write_text(html)
    logger.info("Exported static dashboard to %s", index_path)
    logger.info("File size: %.1f KB", len(html) / 1024)

    print(f"\nStatic dashboard exported to: {index_path}")
    print("To publish on GitHub Pages:")
    print("  1. Push to GitHub")
    print("  2. Settings → Pages → Source: 'Deploy from a branch'")
    print("  3. Branch: main (or headless), folder: /docs")


if __name__ == "__main__":
    export()
