"""
research: Multi-source fact pulling with citations for selected topics.

For each topic with status='selected', pulls:
  - Historical facts from Wikipedia page summary API (primary)
  - Historical facts from Library of Congress search API (secondary)
  - Modern-event facts via Claude API with web search tool

Stores individual factual claims to the `sources` table — one row per fact.
Never stores full scraped article text. This is the copyright risk boundary.

One-shot batch job: runs, researches, stores, exits.
"""

import json
import os
import re
import sqlite3
import sys
import time
import logging
from urllib.parse import quote, urljoin

import requests
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MODEL = "claude-haiku-4-5-20251001"
MAX_RETRIES = 2  # 1 attempt + 1 retry
RETRY_WAIT = 5.0  # seconds, flat wait between retries

HTTP_TIMEOUT = 15  # seconds for Wikipedia/LOC HTTP calls
ANTHROPIC_TIMEOUT = 90.0  # generous — web search adds latency

# Maximum facts to store per source type per topic
MAX_HISTORICAL_FACTS = 5
MAX_MODERN_FACTS = 5
MIN_FACT_LENGTH = 40   # characters — skip sentence fragments
MAX_FACT_LENGTH = 500  # characters — keep facts concise

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[research] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("research")

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


def topic_has_sources(conn: sqlite3.Connection, topic_id: int) -> bool:
    """Return True if this topic already has any source rows (idempotency check)."""
    cursor = conn.execute(
        "SELECT COUNT(*) as cnt FROM sources WHERE topic_id = ?", (topic_id,)
    )
    row = cursor.fetchone()
    return row["cnt"] > 0


def insert_source(
    conn: sqlite3.Connection,
    topic_id: int,
    source_type: str,
    source_name: str,
    source_url: str | None,
    fact_text: str,
    citation_text: str,
) -> int:
    """Insert one fact row into the sources table. Returns the new row id."""
    cursor = conn.execute(
        """INSERT INTO sources
           (topic_id, source_type, source_name, source_url, fact_text, citation_text)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (topic_id, source_type, source_name, source_url or "", fact_text, citation_text),
    )
    conn.commit()
    return cursor.lastrowid

# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------

def check_api_key() -> None:
    """Raise if the Anthropic API key is missing or still a placeholder."""
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY.startswith("placeholder"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set or is still the placeholder value. "
            "Set it in the environment at runtime."
        )

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

def _http_session() -> requests.Session:
    """Return a requests.Session with a descriptive User-Agent."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "HistoryRhymes/1.0 (https://github.com/NicholasGordon2002/History_Rhymes; "
            "mailto:hello@historyrhymes.dev) Python-requests/2"
        ),
    })
    return session

# ---------------------------------------------------------------------------
# Wikipedia page summary API
# ---------------------------------------------------------------------------

def fetch_wikipedia_summary(title: str) -> dict | None:
    """
    Fetch the Wikipedia page summary for a given title.
    Returns a dict with: title, extract, description, content_urls, or None on failure.
    """
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}"
    logger.info("Fetching Wikipedia summary: %s", title)

    for attempt in range(MAX_RETRIES):
        try:
            resp = _http_session().get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code == 404:
                logger.warning("Wikipedia page not found for title: %s", title)
                return None
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "Wikipedia summary: title='%s', extract_len=%d",
                data.get("title", ""), len(data.get("extract", "")),
            )
            return data
        except requests.RequestException as exc:
            logger.warning(
                "Wikipedia API attempt %d/%d for '%s': %s",
                attempt + 1, MAX_RETRIES, title, exc,
            )
            if attempt < MAX_RETRIES - 1:
                logger.info("Retry 1/2 after 5s...")
                time.sleep(RETRY_WAIT)
        except ValueError as exc:
            logger.warning("Wikipedia returned invalid JSON for '%s': %s", title, exc)
            return None

    logger.error("Wikipedia API failed after %d attempts for '%s'", MAX_RETRIES, title)
    return None


def extract_facts_from_text(
    text: str,
    source_name: str,
    source_url: str,
    max_facts: int = MAX_HISTORICAL_FACTS,
) -> list[dict]:
    """
    Split a paragraph of text into individual factual claims (sentences).
    Returns a list of dicts with fact_text and citation_text, ready for the
    sources table. Filters out sentence fragments and very long sentences.

    This deliberately extracts individual claims rather than storing the full
    article text — the copyright risk boundary.
    """
    if not text or not text.strip():
        return []

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Split into sentences (naive but effective for Wikipedia extracts)
    sentences = re.split(r"(?<=[.!?])\s+", text)

    facts = []
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        # Remove Wikipedia footnote markers like [1], [citation needed], etc.
        sent = re.sub(r"\[(citation needed|\d+)\]", "", sent).strip()
        # Skip very short fragments and very long sentences
        if len(sent) < MIN_FACT_LENGTH or len(sent) > MAX_FACT_LENGTH:
            continue
        # Skip sentences that are mostly parenthetical/editorial
        if sent.startswith("(") and sent.endswith(")"):
            continue

        citation = f"Source: {source_name}"
        if source_url:
            citation += f" ({source_url})"

        facts.append({
            "fact_text": sent,
            "citation_text": citation,
        })

        if len(facts) >= max_facts:
            break

    return facts

# ---------------------------------------------------------------------------
# Library of Congress search API
# ---------------------------------------------------------------------------

def fetch_loc_facts(query: str, max_facts: int = 3) -> list[dict]:
    """
    Search the Library of Congress for a topic and extract fact-like snippets.
    The LOC API is free, no key required. Returns a list of fact dicts.

    Uses : https://www.loc.gov/search/?q={query}&fo=json
    """
    url = "https://www.loc.gov/search/"
    params = {"q": query, "fo": "json"}
    logger.info("Searching Library of Congress: %s", query)

    try:
        resp = _http_session().get(url, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("Library of Congress API error for '%s': %s", query, exc)
        return []
    except ValueError as exc:
        logger.warning("Library of Congress JSON parse error for '%s': %s", query, exc)
        return []

    results = data.get("results", [])
    logger.info("LOC returned %d results for '%s'", len(results), query)

    facts = []
    for item in results[:max_facts * 2]:  # fetch extra to filter
        title = item.get("title", "").strip()
        description = item.get("description", [])
        item_url = item.get("url", "")

        # Compose a fact-like sentence from the title and description
        desc_text = ""
        if isinstance(description, list) and description:
            desc_text = description[0].strip()
        elif isinstance(description, str) and description:
            desc_text = description.strip()

        # Build a fact sentence from available metadata
        fact_parts = []
        if title:
            fact_parts.append(title)
        if desc_text:
            fact_parts.append(desc_text)

        fact_text = ". ".join(fact_parts)
        if not fact_text or len(fact_text) < MIN_FACT_LENGTH:
            continue
        if len(fact_text) > MAX_FACT_LENGTH:
            fact_text = fact_text[:MAX_FACT_LENGTH - 3].rsplit(".", 1)[0] + "."

        loc_url = item_url if item_url else f"https://www.loc.gov/search/?q={quote(query)}"
        citation = f"Source: Library of Congress ({loc_url})"

        facts.append({
            "fact_text": fact_text,
            "citation_text": citation,
        })

        if len(facts) >= max_facts:
            break

    logger.info("LOC extracted %d fact(s) for '%s'", len(facts), query)
    return facts

# ---------------------------------------------------------------------------
# Modern-event facts via Claude web search
# ---------------------------------------------------------------------------

MODERN_FACTS_PROMPT = """You are a research assistant for a YouTube Shorts channel called "History Rhymes."
The channel pairs historical events with modern events that echo or contrast them.

Your task: search the web for information about the modern event described below.
Return a JSON object with:
  - "facts": an array of objects, each with:
      "fact_text": a single factual claim (1-2 sentences, concise)
      "source_url": the URL where you found this fact
  - "search_queries_used": array of search query strings you used

Rules:
- Use web search to find factual, verifiable information about the modern event.
- Each fact must be a single, self-contained factual claim.
- Provide the source URL for each fact.
- Return 3-5 facts covering key aspects (what happened, when, who, significance).
- Do NOT fabricate facts — only return what you can verify from web search results.
- Keep facts concise (under 500 characters each).

Return ONLY a JSON object — no other text."""


def fetch_modern_facts_via_claude(topic: sqlite3.Row) -> list[dict]:
    """
    Use Claude with web search to find key facts about the modern event side
    of a topic pairing. Returns a list of fact dicts (fact_text, source_url, citation_text).
    """
    modern_title = (topic["modern_event_title"] or "").strip()
    modern_desc = (topic["modern_event_description"] or "").strip()
    pairing = (topic["pairing_rationale"] or "").strip()

    # Build a search context for Claude
    search_context_parts = []
    if modern_title and modern_title != "Recent Event":
        search_context_parts.append(f"Modern event title: {modern_title}")
    if modern_desc:
        search_context_parts.append(f"Description: {modern_desc[:300]}")
    if pairing:
        search_context_parts.append(f"Context (pairing rationale): {pairing[:200]}")

    if not search_context_parts:
        logger.info("No meaningful modern event info for topic #%d — skipping", topic["id"])
        return []

    search_context = "\n".join(search_context_parts)

    logger.info(
        "Searching modern facts via Claude for topic #%d: %s",
        topic["id"], modern_title[:80],
    )

    user_message = (
        f"Search the web and find key facts about this modern event:\n\n"
        f"{search_context}\n\n"
        "Return your findings as a JSON object with a 'facts' array and "
        "'search_queries_used' array. Only return valid JSON — no other text."
    )

    # --- Diagnostic logging before API call ---
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key and key != "placeholder-anthropic-api-key":
        masked = key[:15] + "..." + key[-4:] if len(key) > 20 else "***too-short***"
        print(f"[research] Using API key: {masked}")
        print(f"[research] API key length: {len(key)} chars, starts with: {key[:10]}...")
    else:
        print("[research] WARNING: ANTHROPIC_API_KEY is missing or still set to placeholder!")
    print(f"[research] Model: {MODEL}")
    print("[research] Endpoint: Anthropic Messages API (client.messages.create)")

    client = Anthropic(
        api_key=ANTHROPIC_API_KEY,
        timeout=ANTHROPIC_TIMEOUT,
        max_retries=0,
    )

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(
                "Claude web search for topic #%d (attempt %d/%d)",
                topic["id"], attempt + 1, MAX_RETRIES,
            )

            response = client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=MODERN_FACTS_PROMPT,
                messages=[{"role": "user", "content": user_message}],
                temperature=0.2,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                tool_choice={"type": "auto"},
            )

            # Response may contain tool_use blocks followed by a text block
            # Find the final text content (after web search results)
            text_content = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text_content = block.text
                    break

            if not text_content:
                # If no text block, look for content in other ways
                logger.warning("No text content in Claude response for topic #%d", topic["id"])
                return []

            # Parse JSON from response
            if "```json" in text_content:
                text_content = text_content.split("```json")[1].split("```")[0].strip()
            elif "```" in text_content:
                text_content = text_content.split("```")[1].split("```")[0].strip()

            result = json.loads(text_content)
            facts = result.get("facts", [])

            # Log search queries used
            queries = result.get("search_queries_used", [])
            if queries:
                logger.info("Claude used %d search queries: %s", len(queries), queries)

            validated_facts = []
            for fact in facts:
                fact_text = (fact.get("fact_text") or "").strip()
                source_url = (fact.get("source_url") or "").strip()

                if not fact_text or len(fact_text) < MIN_FACT_LENGTH:
                    continue
                if len(fact_text) > MAX_FACT_LENGTH:
                    fact_text = fact_text[:MAX_FACT_LENGTH - 3] + "..."

                citation = "Source: Claude web search"
                if source_url:
                    citation += f" ({source_url})"

                validated_facts.append({
                    "fact_text": fact_text,
                    "source_url": source_url,
                    "citation_text": citation,
                })

            logger.info(
                "Claude returned %d validated modern fact(s) for topic #%d",
                len(validated_facts), topic["id"],
            )
            return validated_facts[:MAX_MODERN_FACTS]

        except json.JSONDecodeError as exc:
            last_error = exc
            logger.warning(
                "Attempt %d: Claude returned invalid JSON for topic #%d: %s",
                attempt + 1, topic["id"], exc,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Attempt %d: Claude API error for topic #%d: %s",
                attempt + 1, topic["id"], exc,
            )

        if attempt < MAX_RETRIES - 1:
            logger.info("Retry 1/2 after 5s...")
            time.sleep(RETRY_WAIT)

    logger.error(
        "Claude web search failed after %d attempts for topic #%d. Last error: %s",
        MAX_RETRIES, topic["id"], last_error,
    )
    return []


def fetch_modern_facts_fallback(topic: sqlite3.Row) -> list[dict]:
    """
    When Claude web search is unavailable (e.g., model doesn't support the
    web_search tool), attempt a direct HTTP search fallback using DuckDuckGo
    Instant Answer API. This is a free, no-key API for limited search.

    Returns at most 2 facts from search result snippets.
    """
    modern_title = (topic["modern_event_title"] or "").strip()
    modern_desc = (topic["modern_event_description"] or "").strip()

    query = modern_title if modern_title and modern_title != "Recent Event" else modern_desc[:100]
    if not query.strip():
        return []

    logger.info("Attempting DuckDuckGo fallback for topic #%d: %s", topic["id"], query[:80])

    url = "https://api.duckduckgo.com/"
    params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}

    try:
        resp = _http_session().get(url, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("DuckDuckGo fallback failed for topic #%d: %s", topic["id"], exc)
        return []

    facts = []
    abstract = (data.get("AbstractText") or "").strip()
    abstract_url = (data.get("AbstractURL") or "").strip()

    if abstract and len(abstract) >= MIN_FACT_LENGTH:
        facts.append({
            "fact_text": abstract[:MAX_FACT_LENGTH],
            "source_url": abstract_url,
            "citation_text": f"Source: DuckDuckGo ({abstract_url or 'https://duckduckgo.com/'})",
            })

    # Also check related topics for additional snippets
    related = data.get("RelatedTopics", [])
    for rt in related[:2]:
        if isinstance(rt, dict):
            rt_text = (rt.get("Text") or "").strip()
            rt_url = (rt.get("FirstURL") or "").strip()
            if rt_text and len(rt_text) >= MIN_FACT_LENGTH:
                facts.append({
                    "fact_text": rt_text[:MAX_FACT_LENGTH],
                    "source_url": rt_url,
                    "citation_text": f"Source: DuckDuckGo ({rt_url or 'https://duckduckgo.com/'})",
                })

    logger.info("DuckDuckGo fallback returned %d fact(s) for topic #%d", len(facts), topic["id"])
    return facts[:MAX_MODERN_FACTS]


def fetch_modern_facts(topic: sqlite3.Row) -> list[dict]:
    """
    Fetch modern-event facts. Tries Claude web search first; falls back to
    DuckDuckGo if Claude fails or web search is unsupported by the model.
    """
    # Check if there's actually modern event information to search for
    modern_title = (topic["modern_event_title"] or "").strip()
    modern_desc = (topic["modern_event_description"] or "").strip()
    if (not modern_title or modern_title == "Recent Event") and not modern_desc:
        logger.info(
            "Topic #%d has no meaningful modern event data — skipping modern research",
            topic["id"],
        )
        return []

    # Try Claude web search first
    facts = fetch_modern_facts_via_claude(topic)

    # If Claude web search failed (empty results), try DuckDuckGo fallback
    if not facts:
        logger.info("Claude web search returned no facts for topic #%d — trying fallback", topic["id"])
        facts = fetch_modern_facts_fallback(topic)

    return facts

# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def research_topic(conn: sqlite3.Connection, topic: sqlite3.Row) -> int:
    """
    Research a single topic: pull historical facts from Wikipedia + LOC,
    modern facts via Claude web search, and store everything to the sources table.

    Returns the number of facts stored.
    """
    topic_id = topic["id"]
    title = topic["historical_event_title"]
    logger.info("=== Researching topic #%d: %s ===", topic_id, title[:80])

    total_facts = 0

    # ---- Historical: Wikipedia ----
    wiki_title = title
    # Try to extract a Wikipedia page title from the description if it contains a URL
    desc = topic["historical_event_description"] or ""
    wiki_url_match = re.search(r"source:\s*(https://en\.wikipedia\.org/wiki/(\S+))", desc)
    wiki_page_title = title

    wiki_summary = fetch_wikipedia_summary(wiki_page_title)
    time.sleep(1)  # Rate limit safeguard: Wikipedia requests free API, be respectful

    if wiki_summary:
        wiki_title_display = wiki_summary.get("title", wiki_page_title)
        wiki_url = f"https://en.wikipedia.org/wiki/{quote(wiki_title_display.replace(' ', '_'))}"
        extract = wiki_summary.get("extract", "")

        wiki_facts = extract_facts_from_text(
            extract,
            source_name=f"Wikipedia: {wiki_title_display}",
            source_url=wiki_url,
        )
        for fact in wiki_facts:
            insert_source(
                conn, topic_id, "historical",
                f"Wikipedia: {wiki_title_display}",
                wiki_url,
                fact["fact_text"],
                fact["citation_text"],
            )
            total_facts += 1
        logger.info(
            "Wikipedia: stored %d fact(s) for topic #%d",
            len(wiki_facts), topic_id,
        )
    else:
        logger.warning("Wikipedia summary unavailable for topic #%d: %s", topic_id, title)

    # ---- Historical: Library of Congress (secondary source) ----
    # Use the historical event title as the search query
    loc_query = title[:120]  # reasonable query length
    loc_facts = fetch_loc_facts(loc_query)
    time.sleep(2)  # Rate limit safeguard: Library of Congress requests free API, be respectful
    for fact in loc_facts:
        insert_source(
            conn, topic_id, "historical",
            "Library of Congress",
            f"https://www.loc.gov/search/?q={quote(loc_query)}",
            fact["fact_text"],
            fact["citation_text"],
        )
        total_facts += 1
    logger.info(
        "Library of Congress: stored %d fact(s) for topic #%d",
        len(loc_facts), topic_id,
    )

    # ---- Modern: Claude web search ----
    modern_facts = fetch_modern_facts(topic)
    for fact in modern_facts:
        source_url = fact.get("source_url", "")
        insert_source(
            conn, topic_id, "modern",
            "Claude web search",
            source_url,
            fact["fact_text"],
            fact["citation_text"],
        )
        total_facts += 1
    logger.info(
        "Modern: stored %d fact(s) for topic #%d",
        len(modern_facts), topic_id,
    )

    logger.info("Total %d fact(s) stored for topic #%d", total_facts, topic_id)
    return total_facts


def run():
    """Main entry point: read selected topics, research each, store facts."""
    conn = connect_db()
    cursor = conn.cursor()

    # ---- Read selected topics ----
    cursor.execute(
        "SELECT * FROM topics WHERE status = 'selected' ORDER BY topic_score DESC"
    )
    selected = cursor.fetchall()
    logger.info("Found %d topic(s) with status='selected'", len(selected))

    if not selected:
        logger.info("No selected topics to research — exiting cleanly.")
        conn.close()
        return

    # ---- Mark topics as in_progress ----
    for topic in selected:
        cursor.execute(
            "UPDATE topics SET status = 'in_progress', updated_at = datetime('now') WHERE id = ?",
            (topic["id"],),
        )
    conn.commit()

    # ---- Research each topic ----
    total_facts = 0
    topics_with_issues = 0

    for topic in selected:
        topic_id = topic["id"]

        # Idempotency: skip if already has sources
        if topic_has_sources(conn, topic_id):
            logger.info(
                "Topic #%d already has sources — skipping (idempotent)",
                topic_id,
            )
            continue

        try:
            facts_stored = research_topic(conn, topic)
            total_facts += facts_stored
            if facts_stored == 0:
                topics_with_issues += 1
                logger.warning(
                    "Topic #%d: zero facts stored — may need human review",
                    topic_id,
                )
        except Exception as exc:
            topics_with_issues += 1
            logger.error(
                "Unexpected error researching topic #%d: %s", topic_id, exc,
                exc_info=True,
            )
            # Continue with next topic — don't let one failure block the batch

    conn.close()

    # ---- Summary ----
    summary = {
        "topics_researched": len(selected),
        "total_facts_stored": total_facts,
        "topics_with_issues": topics_with_issues,
        "model": MODEL,
    }
    logger.info("Research run complete: %s", json.dumps(summary))

    if total_facts == 0 and len(selected) > 0:
        logger.error(
            "All %d topics returned zero facts — check API keys and connectivity.",
            len(selected),
        )
        sys.exit(1)


def main():
    try:
        logger.info("Starting research run ...")
        run()
        logger.info("research complete.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
