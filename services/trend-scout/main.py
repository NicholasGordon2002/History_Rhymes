"""
trend-scout: Polls Wikipedia "On This Day" API for candidate historical events.
GDELT integration disabled (unreliable 429 errors) — modern parallels are found
later by the research stage via Claude web search.
Writes candidate rows to the topics table.
One-shot batch job: runs, polls, writes candidates, exits.
"""
import os
import re
import sqlite3
import sys
import time
import logging
from datetime import datetime, timedelta, timezone
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
WIKIPEDIA_BASE = "https://en.wikipedia.org/api/rest_v1/feed/onthisday/events"
# How many days to fetch (today + N-1 ahead)
LOOKAHEAD_DAYS = 3
# Timeouts for external HTTP calls (seconds)
HTTP_TIMEOUT = 15
# Maximum events per day to process (Wikipedia often returns 30-50+)
MAX_EVENTS_PER_DAY = 10
# Cap total candidates written per run (cost-conscious testing; default 3)
MAX_TOPICS = int(os.environ.get("MAX_TOPICS", "3"))

# ---------------------------------------------------------------------------
# PG content filter: block violent/tragic/massacre topics
# ---------------------------------------------------------------------------
CONTENT_FILTER_PATTERNS = [
    r'\bmassacre\b', r'\bmurder(ed|s)?\b', r'\bkilled\b', r'\bslaughter\b',
    r'\bgenocide\b', r'\bholocaust\b', r'\bexecution\b', r'\bassassinat\w+\b',
    r'\bbombing\b', r'\bterroris\w+\b', r'\btorture\b', r'\brape\b',
    r'\bsuicide\b', r'\batomic bomb\b', r'\bnuclear (bomb|weapon)\b',
    r'\bdeath camp\b', r'\bconcentration camp\b', r'\bwar crime\b',
    r'\batrocit\w+\b', r'\bserial killer\b', r'\bshooting\b',
    r'\bbehead\w+\b', r'\bexecuted\b', r'\bhung\b', r'\blinching\b',
    # Disaster / casualty terms (added per Attempt Run #2 feedback)
    r'\bdisaster\b', r'\bcrash\b', r'\bexplosion\b', r'\baccident\b',
    r'\bearthquake\b', r'\bflood\b', r'\btsunami\b', r'\bfire\b',
    r'\boutbreak\b', r'\bepidemic\b', r'\bpandemic\b', r'\bplague\b',
    r'\bfamine\b', r'\bavalanche\b', r'\btornado\b', r'\bhurricane\b',
    r'\bcasualt\w+\b', r'\bdeaths?\b', r'\bperished\b', r'\bfatal\w*\b',
    r'\bwreck\b', r'\bcollapse\b', r'\bsinking\b', r'\bdrowning\b',
]


def is_pg_content(text: str) -> bool:
    """Return True if content passes PG filter (no violence/tragedy)."""
    if not text:
        return True
    text_lower = text.lower()
    for pattern in CONTENT_FILTER_PATTERNS:
        if re.search(pattern, text_lower):
            return False
    return True


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[trend-scout] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("trend-scout")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def connect_db() -> sqlite3.Connection:
    """Connect to SQLite, creating parent directories if needed."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def insert_candidate(conn: sqlite3.Connection, record: dict) -> int:
    """Insert a candidate record into the topics table.
    Returns the new row id.

    Only includes known column keys present in the topics schema
    (except auto-generated ones like id, created_at).
    """
    columns = [
        "historical_event_date",
        "historical_event_title",
        "historical_event_description",
        "modern_event_title",
        "modern_event_description",
        "pairing_rationale",
        "google_trends_momentum",
        "status",
    ]
    # Build the INSERT dynamically to only include provided keys
    provided = {k: v for k, v in record.items() if k in columns}
    placeholders = ", ".join("?" for _ in provided)
    cols = ", ".join(provided.keys())
    values = list(provided.values())
    sql = f"INSERT INTO topics ({cols}) VALUES ({placeholders})"
    cursor = conn.execute(sql, values)
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# HTTP session (shared for connection pooling, proper headers)
# ---------------------------------------------------------------------------
def _http_session() -> requests.Session:
    """Return a requests.Session with a User-Agent that Wikipedia accepts."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "HistoryRhymes/1.0 (https://github.com/NicholasGordon2002/History_Rhymes; "
            "mailto:hello@historyrhymes.dev) Python-requests/2"
        ),
    })
    return session


# ---------------------------------------------------------------------------
# Wikipedia "On This Day" API
# ---------------------------------------------------------------------------
def fetch_on_this_day(month: int, day: int) -> list[dict]:
    """
    Fetch historical events from Wikipedia for a given month/day.
    Returns a list of event dicts, each with: year, text, pages, wiki_url.
    """
    url = f"{WIKIPEDIA_BASE}/{month:02d}/{day:02d}"
    logger.info("Fetching Wikipedia On This Day: %02d/%02d", month, day)
    try:
        resp = _http_session().get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("Wikipedia API error for %02d/%02d: %s", month, day, exc)
        return []
    except ValueError as exc:
        logger.error("Wikipedia API returned invalid JSON for %02d/%02d: %s", month, day, exc)
        return []

    events = data.get("events", [])
    logger.info("Wikipedia returned %d events for %02d/%02d", len(events), month, day)

    # Enrich each event with the primary Wikipedia page URL
    for ev in events:
        pages = ev.get("pages", [])
        if pages:
            url_parts = pages[0].get("content_urls", {}).get("desktop", {})
            ev["wiki_url"] = url_parts.get("page", "")
            ev["wiki_title"] = pages[0].get("titles", {}).get("normalized", pages[0].get("title", ""))
        else:
            ev["wiki_url"] = ""
            ev["wiki_title"] = ""

    return events


# ---------------------------------------------------------------------------
# Modern parallel finder (GDELT disabled — research stage finds parallels)
# ---------------------------------------------------------------------------
def find_modern_parallel(historical_event: dict) -> tuple[str, str, str]:
    """
    GDELT is disabled due to unreliable 429 rate-limiting on free API tier.
    Store all historical events with empty modern fields — the research stage
    will find modern parallels via Claude web search, which is more reliable.
    Returns (modern_title, modern_description, pairing_rationale).
    """
    return ("", "", "pending_modern")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run():
    """Main entry point: fetch events, pair, write candidates."""
    conn = connect_db()
    today = datetime.now(timezone.utc)
    total_events_fetched = 0
    pg_skipped = 0
    filtered_events: list[dict] = []  # collect records that pass PG filter

    for offset in range(LOOKAHEAD_DAYS):
        date = today + timedelta(days=offset)
        month, day = date.month, date.day

        events = fetch_on_this_day(month, day)
        if not events:
            logger.warning("No events returned for %02d/%02d — skipping", month, day)
            continue

        # Process up to MAX_EVENTS_PER_DAY
        for ev in events[:MAX_EVENTS_PER_DAY]:
            total_events_fetched += 1

            year = ev.get("year")
            text = ev.get("text", "").strip()
            wiki_title = ev.get("wiki_title", "")
            wiki_url = ev.get("wiki_url", "")

            if not text:
                continue

            # Build historical event date string: YYYY-MM-DD
            historical_date = f"{year}-{month:02d}-{day:02d}" if year else f"????-{month:02d}-{day:02d}"

            # Compose a title from the Wikipedia page title or first part of text
            title = wiki_title.replace("_", " ") if wiki_title else text[:100]

            # Description: the Wikipedia event text, optionally with the wiki link
            description = text
            if wiki_url:
                description += f" (source: {wiki_url})"

            # --- PG content filter ---
            if not is_pg_content(title) or not is_pg_content(text):
                logger.info("Skipping PG-filtered event: %s", title[:80])
                pg_skipped += 1
                continue

            # Attempt to find a modern parallel
            modern_title, modern_desc, rationale = find_modern_parallel(ev)

            record = {
                "historical_event_date": historical_date,
                "historical_event_title": title,
                "historical_event_description": description,
                "modern_event_title": modern_title or None,
                "modern_event_description": modern_desc or None,
                "pairing_rationale": rationale or "historical_event_pending_modern",
                "google_trends_momentum": None,
                "status": "candidate",
            }
            filtered_events.append(record)

    # --- Cap candidates for cost-conscious testing ---
    candidates_to_write = filtered_events[:MAX_TOPICS]
    logger.info(
        "[trend-scout] Capped at %d candidates (from %d total)",
        MAX_TOPICS,
        len(filtered_events),
    )
    logger.info(
        "[trend-scout] GDELT disabled — storing %d historical events with pending modern status",
        len(candidates_to_write),
    )

    # --- Insert capped candidates ---
    total_candidates = 0
    for record in candidates_to_write:
        try:
            row_id = insert_candidate(conn, record)
            total_candidates += 1
            logger.info(
                "Inserted candidate #%d: %s | modern: %s",
                row_id,
                record["historical_event_title"][:60],
                record["modern_event_title"][:60] if record["modern_event_title"] else "(pending)",
            )
        except Exception as exc:
            logger.error(
                "Failed to insert candidate for '%s': %s",
                record["historical_event_title"][:60],
                exc,
            )

    conn.close()

    logger.info(
        "Done. Fetched %d events, wrote %d candidates across %d day(s).",
        total_events_fetched,
        total_candidates,
        LOOKAHEAD_DAYS,
    )
    logger.info(
        "[trend-scout] PG filter: %d events skipped, %d candidates written",
        pg_skipped,
        total_candidates,
    )

    if total_candidates == 0:
        logger.error("No candidates were written — check upstream APIs.")
        sys.exit(1)


def main():
    try:
        logger.info("Starting trend-scout run...")
        run()
        logger.info("trend-scout complete.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
