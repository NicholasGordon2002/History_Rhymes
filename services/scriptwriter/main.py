"""
scriptwriter: Synthesizes original narration scripts from multi-source facts.

For each topic with status='in_progress' (set by research stage):
  1. Pulls ALL facts from the sources table grouped by source_type
  2. Calls Claude Sonnet to synthesize an original narrative
  3. Stores the script with structured segments to the scripts table
  4. Updates topic status to 'draft'

One-shot batch job: runs, synthesizes, stores, exits.
"""

import json
import os
import random
import sqlite3
import sys
import time
import logging

from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MODEL = "claude-sonnet-5"
MAX_RETRIES = 2  # 1 attempt + 1 retry
RETRY_WAIT = 5.0  # seconds, flat wait between retries
ANTHROPIC_TIMEOUT = 120.0  # generous — Sonnet synthesis can take longer

# YouTube Shorts target: 40–60 seconds of narration at ~2.5 words/sec
TARGET_WORD_COUNT_MIN = 80
TARGET_WORD_COUNT_MAX = 160

# ---------------------------------------------------------------------------
# Structure variation prompts
#
# One is randomly selected per script to ensure each video has a distinct
# narrative framing. This avoids a rigid template that risks YouTube's
# "templated/mass-produced content" policy (compliance requirement #7).
# ---------------------------------------------------------------------------

STRUCTURE_PROMPTS = {
    "contrast-opening": (
        "Open with a striking, visceral contrast between the historical event "
        "and its modern echo. Make the viewer feel the dissonance immediately. "
        "Use the structure: jarring modern fact → historical parallel → "
        "unexpected insight that ties them together."
    ),
    "mystery-reveal": (
        "Start with an intriguing, puzzling, or ominous statement about the "
        "present day. Don't name the historical connection yet. Build suspense "
        "for 2-3 segments, then reveal the historical parallel as the 'answer' "
        "to the mystery. Structure: hook → tension build → reveal → reflection."
    ),
    "parallel-narrative": (
        "Interweave the historical and modern events in parallel, alternating "
        "between timeframes. Use phrases like 'Back in [year]...' and 'Now, in "
        "[year]...' to create a rhythmic back-and-forth. The final segment "
        "should land on what has (or hasn't) changed."
    ),
    "question-first": (
        "Lead with a provocative, challenging question that the viewer can't "
        "help but answer in their head. Then use the historical-modern pairing "
        "to complicate that answer. Structure: question → historical context "
        "→ modern twist → reframed answer."
    ),
    "cause-and-effect": (
        "Show how the historical event set in motion a chain of consequences "
        "that directly shaped the modern event. Emphasize causality and "
        "trajectory — 'this happened because that happened.' Structure: "
        "modern outcome → trace backward to origin → reveal implications."
    ),
    "ironic-juxtaposition": (
        "Place the historical and modern events side by side without explicit "
        "editorializing. Let the irony speak through the juxtaposition itself. "
        "Use a deadpan, matter-of-fact tone. The viewer should reach the "
        "conclusion on their own. Structure: dry historical fact → dry modern "
        "fact → minimal connective tissue → punchy closing line."
    ),
    "what-if-counterfactual": (
        "Pose a brief counterfactual: 'What if [historical event] had gone "
        "differently?' Explore the implications for 1-2 segments, then snap "
        "back to reality and connect to the modern parallel. Structure: "
        "counterfactual hook → alternate timeline glimpse → reality check "
        "→ present-day connection."
    ),
}

# ---------------------------------------------------------------------------
# System prompt — the core of the creative engine
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """You are the head writer for a YouTube Shorts channel called "History Rhymes."
The channel pairs historical events with current events that echo or contrast them —
the editorial hook is "history doesn't repeat, but it rhymes."

Your job: write the narration script for a 40-60 second YouTube Short (roughly
100-150 words of spoken narration) that synthesizes a historical event and a
modern event into a compelling, original narrative.

## CRITICAL RULES

### Copyright compliance
You are given FACTS extracted from multiple independent sources. Facts and
historical events are not copyrightable — only their expression is. Therefore:
- SYNTHESIZE an original narrative structure from the facts. Never rewrite,
  reorganize, or closely paraphrase any single source's wording.
- Use your own words, sentence structures, and narrative framing.
- If a fact appears in only one source, express it in a fundamentally different
  way — different sentence structure, different word choices, different emphasis.

### Authenticity / variety
- Each script must have a distinct structure and voice. NEVER fall into a
  predictable template like "On this day in [year], [event] happened. Meanwhile,
  today..."
- Vary sentence length, pacing, and rhetorical devices between scripts.

### Length
- YouTube Shorts target: 40-60 seconds of narration.
- That's roughly 100-150 spoken words. Stay in this range.
- Every word must earn its place — no filler.

### Visual cues
- Include [VISUAL: ...] markers at natural segment breaks that describe what
  should appear on screen. These guide the visual-sourcing stage.
- Visual cues should reference: historical imagery (photographs, paintings,
  documents), modern footage (news clips, data visualizations, maps, relevant
  b-roll), and text treatments (key dates, quotes, statistics).
- Each visual cue must be a short, concrete description of a scene or subject —
  NOT an abstract concept. "A grainy black-and-white photograph of soldiers
  in a trench" is good. "The weight of history" is not.

### Output format
Return ONLY a JSON object — no other text, no markdown wrapping, no preamble.
The JSON object must have exactly these keys:

{{
  "script_text": "the full continuous narration text (without [VISUAL] markers)",
  "segments": [
    {{
      "order": 1,
      "narration": "text spoken during this segment (1-2 sentences)",
      "visual_cue": "concrete description of what appears on screen",
      "duration_seconds": <estimated seconds, integers 5-15>
    }},
    ...
  ],
  "structure_notes": "brief note on the narrative technique used"
}}

- segments: 4-8 segments, each with a clear visual cue
- duration_seconds must sum to 40-60 total
- script_text is the full narration without any [VISUAL] markers or metadata
  (used directly for voice generation)

## Structure direction for this script

{structure_instruction}

## Topic to write about

{user_message}"""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[scriptwriter] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("scriptwriter")


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


def topic_has_script(conn: sqlite3.Connection, topic_id: int) -> bool:
    """Return True if this topic already has a script row (idempotency check)."""
    cursor = conn.execute(
        "SELECT COUNT(*) as cnt FROM scripts WHERE topic_id = ?", (topic_id,)
    )
    row = cursor.fetchone()
    return row["cnt"] > 0


def fetch_sources_for_topic(
    conn: sqlite3.Connection, topic_id: int
) -> dict[str, list[dict]]:
    """
    Fetch all sources for a topic, grouped by source_type.
    Returns: {"historical": [...], "modern": [...]}
    Each fact dict has: fact_text, source_name, citation_text.
    """
    cursor = conn.execute(
        """SELECT source_type, source_name, fact_text, citation_text, source_url
           FROM sources
           WHERE topic_id = ?
           ORDER BY source_type, id""",
        (topic_id,),
    )
    rows = cursor.fetchall()

    grouped: dict[str, list[dict]] = {"historical": [], "modern": []}
    for row in rows:
        source_type = row["source_type"]
        grouped.setdefault(source_type, []).append({
            "fact_text": row["fact_text"],
            "source_name": row["source_name"],
            "citation_text": row["citation_text"],
            "source_url": row["source_url"],
        })

    return grouped


def insert_script(
    conn: sqlite3.Connection,
    topic_id: int,
    script_text: str,
    segments_json: str,
    structure_notes: str,
) -> int:
    """Insert a script row. Returns the new row id."""
    cursor = conn.execute(
        """INSERT INTO scripts
           (topic_id, script_text, segments_json, structure_notes)
           VALUES (?, ?, ?, ?)""",
        (topic_id, script_text, segments_json, structure_notes),
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
# Fact assembly for the Claude prompt
# ---------------------------------------------------------------------------

def build_topic_prompt(topic: sqlite3.Row, sources: dict[str, list[dict]]) -> str:
    """
    Build a structured prompt block for Claude with the topic metadata
    and all sourced facts.
    """
    parts = []

    # Topic metadata
    parts.append(f"Historical Event: {topic['historical_event_title']}")
    parts.append(f"Date: {topic['historical_event_date']}")
    if topic["historical_event_description"]:
        parts.append(f"Historical context: {topic['historical_event_description'][:400]}")

    parts.append("")
    modern_title = topic["modern_event_title"] or "Recent Event"
    parts.append(f"Modern Event: {modern_title}")
    if topic["modern_event_description"]:
        parts.append(f"Modern context: {topic['modern_event_description'][:400]}")
    if topic["pairing_rationale"]:
        parts.append(f"Pairing rationale: {topic['pairing_rationale'][:300]}")

    # Historical facts
    historical = sources.get("historical", [])
    if historical:
        parts.append("")
        parts.append("--- HISTORICAL FACTS (synthesize, do not copy) ---")
        for i, fact in enumerate(historical, 1):
            parts.append(f"H{i}: {fact['fact_text']}")
            parts.append(f"    Source: {fact['source_name']}")

    # Modern facts
    modern = sources.get("modern", [])
    if modern:
        parts.append("")
        parts.append("--- MODERN FACTS (synthesize, do not copy) ---")
        for i, fact in enumerate(modern, 1):
            parts.append(f"M{i}: {fact['fact_text']}")
            parts.append(f"    Source: {fact['source_name']}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Script synthesis (Claude Sonnet)
# ---------------------------------------------------------------------------

def synthesize_script(
    topic: sqlite3.Row,
    sources: dict[str, list[dict]],
    structure_key: str,
) -> dict:
    """
    Call Claude Sonnet to synthesize a narration script from the topic and facts.

    Returns a dict with: script_text, segments (list), structure_notes.

    Implements 1 attempt + 1 retry with a flat 5-second wait on transient errors and
    JSON parse failures.
    """
    # --- Diagnostic logging before API call ---
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key and key != "placeholder-anthropic-api-key":
        masked = key[:15] + "..." + key[-4:] if len(key) > 20 else "***too-short***"
        print(f"[scriptwriter] Using API key: {masked}")
        print(f"[scriptwriter] API key length: {len(key)} chars, starts with: {key[:10]}...")
    else:
        print("[scriptwriter] WARNING: ANTHROPIC_API_KEY is missing or still set to placeholder!")
    print(f"[scriptwriter] Model: {MODEL}")
    print("[scriptwriter] Endpoint: Anthropic Messages API (client.messages.create)")

    client = Anthropic(
        api_key=ANTHROPIC_API_KEY,
        timeout=ANTHROPIC_TIMEOUT,
        max_retries=0,  # we handle retries ourselves for better logging
    )

    structure_instruction = STRUCTURE_PROMPTS.get(
        structure_key, STRUCTURE_PROMPTS["contrast-opening"]
    )
    user_message = build_topic_prompt(topic, sources)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        structure_instruction=structure_instruction,
        user_message="{user_message}",  # injected separately
    )

    # Build the full message with the topic data
    full_user_message = (
        f"{user_message}\n\n"
        "Synthesize a YouTube Shorts narration script from the facts above. "
        "Return ONLY a valid JSON object — no markdown, no preamble, no other text."
    )

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(
                "Calling Claude Sonnet (%s) for topic #%d with structure '%s' "
                "(attempt %d/%d)",
                MODEL, topic["id"], structure_key, attempt + 1, MAX_RETRIES,
            )

            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": full_user_message}],
                thinking={"type": "disabled"},
            )

            # Find the first text block (skip any thinking blocks)
            content = None
            for block in response.content:
                if hasattr(block, "text"):
                    content = block.text
                    break
            if content is None:
                raise ValueError("No text content in response")

            # Handle potential markdown code fences
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            result = json.loads(content)

            # Validate required fields
            if "script_text" not in result:
                raise ValueError("Response missing 'script_text'")
            if "segments" not in result or not isinstance(result["segments"], list):
                raise ValueError("Response missing valid 'segments' array")

            script_text = result["script_text"]
            segments = result["segments"]
            structure_notes = result.get("structure_notes", structure_key)

            # Basic validation of segments
            for seg in segments:
                if "order" not in seg:
                    raise ValueError(f"Segment missing 'order': {seg}")
                if "narration" not in seg:
                    raise ValueError(f"Segment missing 'narration': {seg}")
                if "visual_cue" not in seg:
                    raise ValueError(f"Segment missing 'visual_cue': {seg}")

            # Word count check — log warning if outside target (but accept)
            word_count = len(script_text.split())
            logger.info(
                "Topic #%d: script synthesized — %d words, %d segments, "
                "structure: %s",
                topic["id"], word_count, len(segments), structure_notes,
            )
            if word_count < TARGET_WORD_COUNT_MIN:
                logger.warning(
                    "Topic #%d: script is short (%d words, target %d-%d). "
                    "May need human review.",
                    topic["id"], word_count, TARGET_WORD_COUNT_MIN, TARGET_WORD_COUNT_MAX,
                )
            elif word_count > TARGET_WORD_COUNT_MAX:
                logger.warning(
                    "Topic #%d: script is long (%d words, target %d-%d). "
                    "May need trimming.",
                    topic["id"], word_count, TARGET_WORD_COUNT_MIN, TARGET_WORD_COUNT_MAX,
                )

            return {
                "script_text": script_text,
                "segments": segments,
                "structure_notes": structure_notes,
            }

        except json.JSONDecodeError as exc:
            last_error = exc
            logger.warning(
                "Attempt %d: Claude returned invalid JSON for topic #%d: %s",
                attempt + 1, topic["id"], exc,
            )
        except (ValueError, KeyError) as exc:
            last_error = exc
            logger.warning(
                "Attempt %d: Claude response missing required fields for "
                "topic #%d: %s",
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

    raise RuntimeError(
        f"Failed to synthesize script for topic #{topic['id']} "
        f"after {MAX_RETRIES} attempts. Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def pick_structure() -> str:
    """Randomly select a structure variation key."""
    keys = list(STRUCTURE_PROMPTS.keys())
    return random.choice(keys)


def process_topic(conn: sqlite3.Connection, topic: sqlite3.Row) -> bool:
    """
    Process a single topic: fetch sources, synthesize script, store result.
    Returns True on success, False on failure.
    """
    topic_id = topic["id"]
    title = topic["historical_event_title"]
    logger.info("=== Synthesizing script for topic #%d: %s ===", topic_id, title[:80])

    # Idempotency: skip if already has a script
    if topic_has_script(conn, topic_id):
        logger.info("Topic #%d already has a script — skipping (idempotent)", topic_id)
        # Still mark as draft if it's in_progress
        conn.execute(
            "UPDATE topics SET status = 'draft', updated_at = datetime('now') "
            "WHERE id = ? AND status = 'in_progress'",
            (topic_id,),
        )
        conn.commit()
        return True

    # Fetch sources
    sources = fetch_sources_for_topic(conn, topic_id)
    total_facts = len(sources.get("historical", [])) + len(sources.get("modern", []))

    if total_facts == 0:
        logger.warning(
            "Topic #%d has zero facts in the sources table — "
            "nothing to synthesize. Skipping.",
            topic_id,
        )
        return False

    historical_count = len(sources.get("historical", []))
    modern_count = len(sources.get("modern", []))
    logger.info(
        "Topic #%d: %d historical facts, %d modern facts",
        topic_id, historical_count, modern_count,
    )

    if historical_count == 0:
        logger.warning(
            "Topic #%d: no historical facts available. "
            "Script may be unbalanced — proceeding with what we have.",
            topic_id,
        )
    if modern_count == 0:
        logger.warning(
            "Topic #%d: no modern facts available. "
            "Script will focus on historical event alone.",
            topic_id,
        )

    # Pick a random structure variation
    structure_key = pick_structure()
    logger.info("Topic #%d: selected structure '%s'", topic_id, structure_key)

    # Synthesize
    try:
        result = synthesize_script(topic, sources, structure_key)
    except Exception as exc:
        logger.error("Script synthesis failed for topic #%d: %s", topic_id, exc)
        return False

    # Store script
    segments_json = json.dumps(result["segments"], ensure_ascii=False)
    script_id = insert_script(
        conn,
        topic_id,
        result["script_text"],
        segments_json,
        result["structure_notes"],
    )
    logger.info("Topic #%d: stored script row id=%d", topic_id, script_id)

    # Update topic status to draft
    conn.execute(
        "UPDATE topics SET status = 'draft', updated_at = datetime('now') "
        "WHERE id = ?",
        (topic_id,),
    )
    conn.commit()
    logger.info("Topic #%d: status updated to 'draft'", topic_id)

    return True


def run():
    """Main entry point: read in-progress topics, synthesize scripts, store."""
    check_api_key()

    conn = connect_db()
    cursor = conn.cursor()

    # Read topics with status='in_progress', ordered by score (best first)
    cursor.execute(
        "SELECT * FROM topics WHERE status = 'in_progress' ORDER BY topic_score DESC"
    )
    topics = cursor.fetchall()
    logger.info("Found %d topic(s) with status='in_progress'", len(topics))

    if not topics:
        logger.info("No in-progress topics to script — exiting cleanly.")
        conn.close()
        return

    # Process each topic
    successes = 0
    failures = 0

    for topic in topics:
        try:
            if process_topic(conn, topic):
                successes += 1
            else:
                failures += 1
        except Exception as exc:
            failures += 1
            logger.error(
                "Unexpected error processing topic #%d: %s",
                topic["id"], exc, exc_info=True,
            )
            # Continue with next topic — don't let one failure block the batch

    conn.close()

    # Summary
    summary = {
        "model": MODEL,
        "topics_processed": len(topics),
        "scripts_synthesized": successes,
        "failures": failures,
    }
    logger.info("Scriptwriter run complete: %s", json.dumps(summary))

    if successes == 0 and len(topics) > 0:
        logger.error(
            "All %d topics failed script synthesis — check API key and connectivity.",
            len(topics),
        )
        sys.exit(1)


def main():
    try:
        logger.info("Starting scriptwriter run ...")
        run()
        logger.info("scriptwriter complete.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
