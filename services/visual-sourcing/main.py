"""
visual-sourcing: Matches script segments to public-domain and open-license visuals.

For each topic with status='draft' (scripts written but visuals not yet sourced):
  1. Pulls the script's segments_json and parses the visual_cue from each segment
  2. Searches Wikimedia Commons and Library of Congress for matching public-domain images
  3. Stores matched assets in the visual_assets table with full attribution

Sources (all free, no API key required):
  - Wikimedia Commons API: https://commons.wikimedia.org/w/api.php
  - Library of Congress: https://www.loc.gov/photos/

One-shot batch job: runs, sources, stores, exits.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
from urllib.parse import quote_plus, urljoin

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
STOCK_LIBRARY_API_KEY = os.environ.get("STOCK_LIBRARY_API_KEY", "")

# Timeout for external HTTP requests
REQUEST_TIMEOUT = 15.0
# Friendly delay between API calls to avoid rate limiting
API_DELAY = 0.5

# Wikimedia requires a descriptive User-Agent (per their API etiquette policy)
USER_AGENT = "HistoryRhymes/1.0 (https://github.com/NicholasGordon2002/History_Rhymes; visual-sourcing bot; contact via repo)"
HEADERS = {"User-Agent": USER_AGENT}

# Wikimedia Commons API endpoint
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"

# Library of Congress API endpoint
LOC_API = "https://www.loc.gov/photos/"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[visual-sourcing] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("visual-sourcing")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def connect_db() -> sqlite3.Connection:
    """Open the shared SQLite database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def get_draft_scripts(conn: sqlite3.Connection):
    """Return all scripts whose topics have status='draft' and no visual assets yet."""
    cursor = conn.execute(
        """
        SELECT s.id AS script_id, s.topic_id, s.segments_json, s.script_text,
               t.historical_event_title, t.modern_event_title
        FROM scripts s
        JOIN topics t ON s.topic_id = t.id
        WHERE t.status = 'draft'
        ORDER BY s.topic_id
        """
    )
    return cursor.fetchall()


def topic_has_assets(conn: sqlite3.Connection, topic_id: int) -> bool:
    """Return True if this topic already has visual asset rows (idempotency check)."""
    cursor = conn.execute(
        "SELECT COUNT(*) AS cnt FROM visual_assets WHERE topic_id = ?", (topic_id,)
    )
    row = cursor.fetchone()
    return row["cnt"] > 0


def insert_visual_asset(
    conn: sqlite3.Connection,
    topic_id: int,
    segment_index: int,
    asset_url: str,
    asset_source: str,
    license_tier: str,
    attribution_text: str,
):
    """Insert a visual asset row into the database."""
    conn.execute(
        """
        INSERT INTO visual_assets (topic_id, script_segment_index, asset_url,
                                   asset_source, license_tier, attribution_text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (topic_id, segment_index, asset_url, asset_source, license_tier, attribution_text),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Visual cue parsing
# ---------------------------------------------------------------------------

def strip_visual_cue(cue: str) -> str:
    """Remove [VISUAL: ...] wrapper if present, return clean search query."""
    cue = cue.strip()
    # Match [VISUAL: ...] or [VISUAL ...] patterns
    m = re.match(r'^\[VISUAL\s*:?\s*(.*?)\]$', cue, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return cue


# ---------------------------------------------------------------------------
# Image sourcing — Wikimedia Commons
# ---------------------------------------------------------------------------

def search_wikimedia_commons(query: str, limit: int = 5) -> list[dict]:
    """
    Search Wikimedia Commons for images matching the query.
    Returns a list of dicts with keys: url, attribution, license_tier
    """
    results = []
    try:
        # Step 1: Search for file pages
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": "6",  # File namespace only
            "srlimit": str(limit),
            "format": "json",
        }
        resp = requests.get(WIKIMEDIA_API, params=params, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()

        search_results = data.get("query", {}).get("search", [])
        if not search_results:
            logger.info(f"  Wikimedia: no results for '{query}'")
            return results

        # Step 2: Get image URLs for found files
        titles = "|".join(r["title"] for r in search_results)
        img_params = {
            "action": "query",
            "titles": titles,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "format": "json",
        }
        time.sleep(API_DELAY)
        img_resp = requests.get(WIKIMEDIA_API, params=img_params, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        img_resp.raise_for_status()
        img_data = img_resp.json()

        pages = img_data.get("query", {}).get("pages", {})
        for page_id, page in pages.items():
            if page_id == "-1":
                continue
            imageinfo = page.get("imageinfo", [])
            if not imageinfo:
                continue
            info = imageinfo[0]
            img_url = info.get("url", "")
            if not img_url:
                continue

            # Determine license from metadata
            ext_meta = info.get("extmetadata", {})
            license_tier = determine_wikimedia_license(ext_meta)
            attribution = build_attribution(page.get("title", ""), ext_meta)

            results.append({
                "url": img_url,
                "attribution": attribution,
                "license_tier": license_tier,
                "source": "Wikimedia Commons",
            })

        logger.info(f"  Wikimedia: {len(results)} image(s) for '{query}'")
    except requests.RequestException as e:
        logger.warning(f"  Wikimedia API error: {e}")
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.warning(f"  Wikimedia parse error: {e}")

    return results


def determine_wikimedia_license(ext_meta: dict) -> str:
    """Heuristic to classify license tier from Wikimedia metadata."""
    license_text = ""
    usage_terms = ext_meta.get("License", {}).get("value", "")
    copyright_str = ext_meta.get("Copyrighted", {}).get("value", "")
    artist = ext_meta.get("Artist", {}).get("value", "")

    # Check for explicit CC0 / Public Domain markers
    combined = f"{usage_terms} {copyright_str}".lower()

    if any(t in combined for t in ["cc0", "public domain", "cc-zero"]):
        return "CC0"
    if "pd-" in combined or "public domain" in combined:
        return "public_domain"
    # If there's an artist/author but no explicit PD/CC0, default to public_domain
    # for Commons — most hosted images are freely licensed, and we're using
    # the search namespace=6 which restricts to file pages
    if artist and "true" not in copyright_str.lower():
        return "public_domain"
    # Default for Commons: treat as public domain (Wikimedia Commons only
    # hosts freely-licensed or PD content)
    return "public_domain"


def build_attribution(title: str, ext_meta: dict) -> str:
    """Build an attribution string from Wikimedia metadata."""
    artist = ext_meta.get("Artist", {}).get("value", "")
    license_short = ext_meta.get("LicenseShortName", {}).get("value", "")

    parts = []
    if title:
        parts.append(title.replace("File:", ""))
    if artist and artist != "Unknown" and "{{" not in artist:
        # Clean HTML from artist field
        artist_clean = re.sub(r'<[^>]+>', '', artist).strip()
        if artist_clean:
            parts.append(f"by {artist_clean}")
    if license_short:
        parts.append(f"({license_short})")
    parts.append("via Wikimedia Commons")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Image sourcing — Library of Congress
# ---------------------------------------------------------------------------

def search_library_of_congress(query: str, limit: int = 5) -> list[dict]:
    """
    Search the Library of Congress photo archive.
    Returns a list of dicts with keys: url, attribution, license_tier
    """
    results = []
    try:
        params = {
            "q": query,
            "fo": "json",
            "c": str(limit),
        }
        resp = requests.get(LOC_API, params=params, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()

        items = []
        # The LOC JSON response structure can vary; try common paths
        if "results" in data:
            items = data["results"]
        elif "items" in data:
            items = data["items"]
        elif isinstance(data, list):
            items = data[:limit]

        if not items:
            logger.info(f"  LOC: no results for '{query}'")
            return results

        for item in items:
            # Extract image URL — LOC items have various structures;
            # some fields may be lists instead of strings
            img_url = ""

            def _first_string(val):
                """Return the first string from a value that could be str, list, or None."""
                if isinstance(val, str):
                    return val
                if isinstance(val, list) and len(val) > 0:
                    return str(val[0]) if not isinstance(val[0], str) else val[0]
                return ""

            if "image_url" in item:
                img_url = _first_string(item["image_url"])
            elif "image" in item and isinstance(item["image"], dict):
                img_url = item["image"].get("full", "") or item["image"].get("url", "")
            elif "url" in item:
                img_url = _first_string(item["url"])
            # Try nested result structures
            if not img_url and "thumbnail_url" in item:
                img_url = _first_string(item["thumbnail_url"])
            if not img_url and "id" in item:
                # Try to construct a known LOC image URL pattern
                item_id = item.get("id", "")
                if item_id:
                    img_url = f"https://tile.loc.gov/storage-services/service/pnp/{item_id}.jpg"

            if not img_url or not img_url.startswith("http"):
                continue

            title = item.get("title", "Untitled")
            creator = item.get("creator", "") or item.get("photographer", "")
            date = item.get("date", "")

            attribution_parts = [title]
            if creator:
                attribution_parts.append(f"by {creator}")
            if date:
                attribution_parts.append(f"({date})")
            attribution_parts.append("Library of Congress")

            # LOC photos are generally public domain (government-produced or pre-1923)
            results.append({
                "url": img_url,
                "attribution": " | ".join(attribution_parts),
                "license_tier": "public_domain",
                "source": "Library of Congress",
            })

        logger.info(f"  LOC: {len(results)} image(s) for '{query}'")
    except requests.RequestException as e:
        logger.warning(f"  LOC API error: {e}")
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.warning(f"  LOC parse error: {e}")

    return results


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_segments(conn: sqlite3.Connection, topic_id: int, segments: list[dict]):
    """For each segment, search for visuals and store results."""
    for segment in segments:
        segment_order = segment.get("order", 0)
        visual_cue_raw = segment.get("visual_cue", "")
        if not visual_cue_raw:
            logger.info(f"  Segment {segment_order}: no visual_cue, skipping")
            continue

        query = strip_visual_cue(visual_cue_raw)
        logger.info(f"  Segment {segment_order}: searching for '{query}'")

        # Search both sources
        commons_results = search_wikimedia_commons(query)
        time.sleep(1)  # Rate limit safeguard: Wikimedia Commons requests free API, be respectful
        loc_results = search_library_of_congress(query)
        time.sleep(2)  # Rate limit safeguard: Library of Congress requests free API, be respectful

        # Combine results — prefer Commons first then LOC
        all_results = commons_results + loc_results

        if not all_results:
            logger.info(f"  Segment {segment_order}: no images found for '{query}'")
            # Store a placeholder entry so the pipeline can track incomplete segments
            insert_visual_asset(
                conn, topic_id, segment_order,
                asset_url="",
                asset_source="none_available",
                license_tier="public_domain",
                attribution_text=f"No free image found for: {query}",
            )
            continue

        # Take the first result for now (assembly will have one image per segment)
        best = all_results[0]
        insert_visual_asset(
            conn, topic_id, segment_order,
            asset_url=best["url"],
            asset_source=best["source"],
            license_tier=best["license_tier"],
            attribution_text=best["attribution"],
        )
        logger.info(f"  Segment {segment_order}: matched → {best['source']} ({best['license_tier']})")


def main():
    logger.info("Starting visual-sourcing run...")

    try:
        conn = connect_db()
    except sqlite3.Error as e:
        logger.error(f"Database connection failed: {e}")
        sys.exit(1)

    try:
        scripts = get_draft_scripts(conn)
        if not scripts:
            logger.info("No draft scripts found. Nothing to do.")
            return

        logger.info(f"Found {len(scripts)} script(s) to source visuals for.")

        for script in scripts:
            topic_id = script["topic_id"]
            script_id = script["script_id"]

            # Idempotency: skip topics that already have visual assets
            if topic_has_assets(conn, topic_id):
                logger.info(f"Topic {topic_id}: already has visual assets, skipping")
                continue

            # Parse segments JSON
            segments_json = script["segments_json"]
            if not segments_json:
                logger.warning(f"Topic {topic_id}: no segments_json in script {script_id}, skipping")
                continue

            try:
                parsed = json.loads(segments_json)
                segments = parsed.get("segments", [])
            except (json.JSONDecodeError, TypeError) as e:
                logger.error(f"Topic {topic_id}: failed to parse segments_json: {e}")
                continue

            if not segments:
                logger.warning(f"Topic {topic_id}: empty segments list, skipping")
                continue

            logger.info(
                f"Topic {topic_id}: processing {len(segments)} segment(s) "
                f"({script['historical_event_title']})"
            )

            process_segments(conn, topic_id, segments)

        logger.info("visual-sourcing run complete.")

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
