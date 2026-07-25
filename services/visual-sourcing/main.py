"""
visual-sourcing: Matches script segments to public-domain visuals via
Wikimedia Commons API. One-shot batch job.

For each topic with status='draft':
  1. Joins with the scripts table to get segments_json
  2. Parses each segment's visual_cue field to form search queries
  3. Searches Wikimedia Commons for matching images
  4. Stores matched assets in the visual_assets table with attribution
"""

import json
import os
import re
import sqlite3
import sys
import time
import logging
from urllib.parse import quote_plus

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")

# Wikimedia Commons API endpoint
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"

# Search result limit per segment (the pipeline will use up to 2 images)
SR_LIMIT = 2

# Friendly delay between API calls to avoid rate limiting
API_DELAY = 0.5
# Timeout for external HTTP requests
REQUEST_TIMEOUT = 15.0

# Wikimedia requires a descriptive User-Agent per their API etiquette policy
USER_AGENT = (
    "HistoryRhymes/1.0 "
    "(https://github.com/NicholasGordon2002/History_Rhymes; "
    "visual-sourcing bot; contact via repo)"
)

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


def topic_has_assets(conn: sqlite3.Connection, topic_id: int) -> bool:
    """Return True if this topic already has visual_assets rows (idempotency)."""
    cursor = conn.execute(
        "SELECT COUNT(*) AS cnt FROM visual_assets WHERE topic_id = ?",
        (topic_id,),
    )
    row = cursor.fetchone()
    return row["cnt"] > 0


# ---------------------------------------------------------------------------
# Visual cue parsing
# ---------------------------------------------------------------------------

_VISUAL_RE = re.compile(r"^\[VISUAL\s*:?\s*(.*?)\]$", re.IGNORECASE)


def strip_visual_cue(raw_cue: str) -> str:
    """
    Remove [VISUAL: ...] wrapper if present, returning a clean search query.
    Handles: [VISUAL: description], [VISUAL description], and bare descriptions.
    """
    cue = raw_cue.strip()
    m = _VISUAL_RE.match(cue)
    if m:
        return m.group(1).strip()
    return cue


# ---------------------------------------------------------------------------
# Wikimedia Commons API
# ---------------------------------------------------------------------------

def search_wikimedia_commons(query: str) -> list[dict]:
    """
    Search Wikimedia Commons for images matching the query.

    Two-step process:
      1. Search for file pages (namespace 6) via list=search
      2. Resolve page titles to actual image URLs via prop=imageinfo

    Returns a list of dicts, each with: url, title, license_tier, attribution.
    Returns an empty list on any error or no results.
    """
    results: list[dict] = []

    # --- Step 1: search for file pages ---
    try:
        search_params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": "6",       # File namespace only
            "srlimit": str(SR_LIMIT),
            "format": "json",
        }
        resp = requests.get(
            WIKIMEDIA_API,
            params=search_params,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("Wikimedia search request failed for '%s': %s", query, exc)
        return results
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Wikimedia search parse error for '%s': %s", query, exc)
        return results

    search_results = data.get("query", {}).get("search", [])
    if not search_results:
        logger.info("Wikimedia: no results for '%s'", query)
        return results

    # --- Step 2: resolve titles to image URLs ---
    titles = "|".join(r["title"] for r in search_results)

    # Rate-limit courtesy delay
    time.sleep(API_DELAY)

    try:
        img_params = {
            "action": "query",
            "titles": titles,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "format": "json",
        }
        img_resp = requests.get(
            WIKIMEDIA_API,
            params=img_params,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        img_resp.raise_for_status()
        img_data = img_resp.json()
    except requests.RequestException as exc:
        logger.warning("Wikimedia imageinfo request failed for '%s': %s", query, exc)
        return results
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Wikimedia imageinfo parse error for '%s': %s", query, exc)
        return results

    pages = img_data.get("query", {}).get("pages", {})

    for _page_id, page in pages.items():
        if _page_id == "-1":
            continue

        imageinfo_list = page.get("imageinfo", [])
        if not imageinfo_list:
            continue

        info = imageinfo_list[0]
        img_url = info.get("url", "")
        if not img_url:
            continue

        ext_meta = info.get("extmetadata", {})
        license_tier = _classify_license(ext_meta)
        attribution = _build_attribution(
            page.get("title", ""), ext_meta
        )

        results.append({
            "url": img_url,
            "title": page.get("title", ""),
            "license_tier": license_tier,
            "attribution": attribution,
        })

    logger.info("Wikimedia: %d image(s) for '%s'", len(results), query)
    return results


def _classify_license(ext_meta: dict) -> str:
    """
    Determine license_tier from Wikimedia extmetadata.

    Wikimedia Commons only hosts freely-licensed or public-domain content,
    so everything is either 'public_domain' or 'CC0'. We examine the
    License and Copyrighted metadata fields to distinguish.
    """
    license_val = ext_meta.get("License", {}).get("value", "")
    copyrighted = ext_meta.get("Copyrighted", {}).get("value", "")
    combined = f"{license_val} {copyrighted}".lower()

    # Explicit CC0
    if any(t in combined for t in ("cc0", "cc-zero", "cc zero")):
        return "CC0"

    # Explicit public domain
    if any(t in combined for t in ("public domain", "pd-mark", "pd-old", "pd-us")):
        return "public_domain"

    # Commons default: public domain (only PD/free content is hosted)
    return "public_domain"


def _build_attribution(title: str, ext_meta: dict) -> str:
    """Build a human-readable attribution string from Commons metadata."""
    artist = ext_meta.get("Artist", {}).get("value", "")
    license_short = ext_meta.get("LicenseShortName", {}).get("value", "")

    parts = []
    clean_title = title.replace("File:", "").strip()
    if clean_title:
        parts.append(clean_title)

    if artist and artist != "Unknown":
        # Strip HTML tags from artist field
        artist_clean = re.sub(r"<[^>]+>", "", artist).strip()
        if artist_clean and "{{" not in artist_clean:
            parts.append(f"by {artist_clean}")

    if license_short:
        parts.append(f"({license_short})")

    parts.append("via Wikimedia Commons")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run():
    """Main entry point: read draft scripts, source visuals, store assets."""
    conn = connect_db()

    # --- Read draft scripts (topics with status='draft', joined with scripts) ---
    cursor = conn.execute(
        """
        SELECT s.id AS script_id, s.topic_id, s.segments_json,
               t.historical_event_title, t.modern_event_title
        FROM scripts s
        JOIN topics t ON s.topic_id = t.id
        WHERE t.status = 'draft'
        ORDER BY t.topic_score DESC
        """
    )
    scripts = cursor.fetchall()
    logger.info("Found %d draft script(s) to source visuals for", len(scripts))

    if not scripts:
        logger.info("No draft topics — exiting cleanly.")
        conn.close()
        return

    total_assets = 0

    for script in scripts:
        topic_id = script["topic_id"]
        script_id = script["script_id"]

        # --- Idempotency: skip if already has assets ---
        if topic_has_assets(conn, topic_id):
            logger.info("Topic #%d already has visual assets — skipping", topic_id)
            continue

        # --- Parse segments_json ---
        segments_json = script["segments_json"]
        if not segments_json:
            logger.warning(
                "Topic #%d (script #%d): no segments_json — skipping",
                topic_id, script_id,
            )
            continue

        try:
            parsed = json.loads(segments_json)
            segments = parsed.get("segments", [])
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error(
                "Topic #%d: failed to parse segments_json: %s", topic_id, exc
            )
            continue

        if not segments:
            logger.warning(
                "Topic #%d: empty segments list — skipping", topic_id
            )
            continue

        logger.info(
            "Topic #%d (%s): processing %d segment(s)",
            topic_id, (script["historical_event_title"] or "")[:60],
            len(segments),
        )

        # --- Process each segment ---
        for segment in segments:
            segment_order = segment.get("order", 0)
            visual_cue_raw = segment.get("visual_cue", "")

            if not visual_cue_raw:
                logger.info(
                    "  Segment %d: no visual_cue — skipping", segment_order
                )
                continue

            query = strip_visual_cue(visual_cue_raw)
            logger.info("  Segment %d: searching '%s'", segment_order, query)

            # Search Wikimedia Commons
            matches = search_wikimedia_commons(query)

            if not matches:
                logger.warning(
                    "  Segment %d: no images found for '%s'", segment_order, query
                )
                # Store a placeholder so the pipeline can track unsourced segments
                conn.execute(
                    """INSERT INTO visual_assets
                       (topic_id, script_segment_index, asset_url, asset_source,
                        license_tier, attribution_text)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        topic_id, segment_order,
                        "", "none_available", "public_domain",
                        f"No free image found for: {query}",
                    ),
                )
                conn.commit()
                total_assets += 1
                continue

            # Insert one row per matched image
            for match in matches:
                conn.execute(
                    """INSERT INTO visual_assets
                       (topic_id, script_segment_index, asset_url, asset_source,
                        license_tier, attribution_text)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        topic_id, segment_order,
                        match["url"], "Wikimedia Commons",
                        match["license_tier"], match["attribution"],
                    ),
                )
                conn.commit()
                total_assets += 1
                logger.info(
                    "  Segment %d: stored %s (%s)",
                    segment_order, match["license_tier"],
                    match["attribution"][:80],
                )

    conn.close()

    summary = {
        "draft_scripts": len(scripts),
        "total_assets_stored": total_assets,
        "source": "Wikimedia Commons",
    }
    logger.info("Visual-sourcing complete: %s", json.dumps(summary))


def main():
    try:
        logger.info("Starting visual-sourcing run ...")
        run()
        logger.info("visual-sourcing complete.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
