"""
topic-scorer: Scores candidate topics via Claude Haiku API and selects the day's
best pairings for production. One-shot batch job.

Reads all candidates (status='candidate'), batches them into a single Claude
API call, then marks the top-N by score as 'selected'.
"""

import json
import os
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
TOP_N = int(os.environ.get("TOP_N", "5"))
# Cap selected candidates per run (cost-conscious testing; default 3)
MAX_TOPICS = int(os.environ.get("MAX_TOPICS", "3"))
MODEL = "claude-3-haiku-20240307"
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # seconds, multiplied exponentially
ANTHROPIC_TIMEOUT = 60.0  # generous timeout for batch scoring

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[topic-scorer] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("topic-scorer")


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


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------

def check_api_key() -> None:
    """Raise if the API key is missing or still a placeholder."""
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY.startswith("placeholder"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set or is still the placeholder value. "
            "Set it in the environment at runtime."
        )


# ---------------------------------------------------------------------------
# Candidate assembly
# ---------------------------------------------------------------------------

def build_candidates_payload(candidates: list[sqlite3.Row]) -> list[dict]:
    """
    Convert DB rows into a list of dicts suitable for the Claude prompt.
    Truncates long descriptions to keep token usage reasonable.
    """
    payload = []
    for row in candidates:
        hist_desc = (row["historical_event_description"] or "")[:500]
        modern_title = row["modern_event_title"] or ""
        modern_desc = (row["modern_event_description"] or "")[:500]

        # Detect placeholder modern fields
        has_modern = bool(
            modern_title.strip()
            and modern_title.strip() != "Recent Event"
            and "pending" not in modern_title.lower()
            and "pending" not in (row["pairing_rationale"] or "").lower()
        )

        payload.append({
            "id": row["id"],
            "historical_event_date": row["historical_event_date"],
            "historical_event_title": row["historical_event_title"],
            "historical_event_description": hist_desc,
            "modern_event_title": modern_title or "(no modern parallel identified yet)",
            "modern_event_description": modern_desc or "",
            "pairing_rationale": (row["pairing_rationale"] or "")[:300],
            "modern_side_weak": not has_modern,
        })

    return payload


# ---------------------------------------------------------------------------
# Scoring (Claude Haiku API)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an editorial scoring system for a YouTube Shorts channel called "History Rhymes."
The channel pairs historical events that occurred "on this day" with current events
that echo or contrast them — the hook is "history doesn't repeat, but it rhymes."

Your job: score a batch of candidate topic pairings on their potential to earn
attention as a 60-second YouTube Short.

Evaluation criteria (each 0.0-1.0 scale):
- emotional_resonance: How strongly will this pairing evoke emotion
  (anger, awe, nostalgia, irony, schadenfreude, hope, outrage, dark humor)?
- pairing_novelty: How fresh, surprising, or clever is the connection between
  the historical and modern events? Avoid giving high novelty to obvious pairings.
- rhyme_clarity: How clear and instant is the "rhyme"? Would a viewer grasp the
  connection within the first 3 seconds without explanation?
- attention_potential: Holistic score — likelihood this stops a viewer scrolling
  and holds them for the full 60 seconds. Consider the YouTuber Shorts audience.

Rules:
- Score these topics for a PG-rated educational channel. Topics involving
  violence, massacres, genocide, terrorism, executions, war, or graphic tragedy
  should receive very low scores (0.0-0.1 overall). Prefer topics about
  discovery, invention, culture, sports, politics (non-violent), science,
  arts, and human achievement.
- A candidate with "modern_side_weak": true means we haven't found a strong
  modern parallel yet — score the historical event on its standalone merits and
  note the gap. Don't automatically penalize these; sometimes the history alone
  is compelling.
- Be decisive. Strong candidates should score 0.7-0.9; weak ones 0.2-0.4.
  Never give every candidate a 0.5. The scores must help us pick winners.
- For each candidate, provide a one-sentence justification.

Return ONLY a JSON object with a "results" array. Each element must have:
  "id": int,
  "emotional_resonance": float (0-1),
  "pairing_novelty": float (0-1),
  "rhyme_clarity": float (0-1),
  "attention_potential": float (0-1),
  "overall_score": float (average of the four above, 0-1),
  "justification": string (one sentence)
No other text outside the JSON."""


def score_candidates(payload: list[dict]) -> dict[int, dict]:
    """
    Send the full candidate list to Claude Haiku for scoring.
    Returns a dict mapping candidate id → scores dict.

    Implements retry with exponential backoff on transient errors.
    """
    client = Anthropic(
        api_key=ANTHROPIC_API_KEY,
        timeout=ANTHROPIC_TIMEOUT,
        max_retries=0,  # we handle retries ourselves for better logging
    )

    user_message = (
        f"Score these {len(payload)} candidate topic pairings:\n\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "Return your scores as a single JSON object with a 'results' array. "
        "Only return valid JSON — no other text."
    )

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(
                "Calling Claude Haiku (%s) with %d candidates (attempt %d/%d)",
                MODEL, len(payload), attempt + 1, MAX_RETRIES,
            )

            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
                temperature=0.3,
            )

            # Extract text from the response
            content = response.content[0].text

            # Handle potential markdown code fences
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            result = json.loads(content)

            if "results" not in result or not isinstance(result["results"], list):
                raise ValueError("Claude response missing 'results' array")

            # Build dict keyed by id, validate scores
            scored: dict[int, dict] = {}
            for item in result["results"]:
                cid = item["id"]
                # Clamp scores to 0-1 range
                for key in (
                    "emotional_resonance", "pairing_novelty",
                    "rhyme_clarity", "attention_potential", "overall_score",
                ):
                    if key in item:
                        item[key] = round(max(0.0, min(1.0, float(item[key]))), 3)
                scored[cid] = item

            logger.info("Claude returned scores for %d candidates", len(scored))
            return scored

        except json.JSONDecodeError as exc:
            last_error = exc
            logger.warning(
                "Attempt %d: Claude returned invalid JSON: %s", attempt + 1, exc
            )
        except Exception as exc:
            last_error = exc
            logger.warning("Attempt %d: Claude API error: %s", attempt + 1, exc)

        if attempt < MAX_RETRIES - 1:
            wait = RETRY_BACKOFF ** (attempt + 1)
            logger.info("Retrying in %.1fs ...", wait)
            time.sleep(wait)

    raise RuntimeError(
        f"Failed to score candidates after {MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run():
    """Main entry point: read candidates, score, select top N, update DB."""
    check_api_key()

    conn = connect_db()
    cursor = conn.cursor()

    # --- Read candidates ---
    cursor.execute(
        "SELECT * FROM topics WHERE status = 'candidate' ORDER BY created_at DESC"
    )
    candidates = cursor.fetchall()
    logger.info("Found %d candidate(s) with status='candidate'", len(candidates))

    if not candidates:
        logger.info("No candidates to score — exiting cleanly.")
        conn.close()
        return

    # --- Score ---
    payload = build_candidates_payload(candidates)
    scored = score_candidates(payload)

    # --- Select top N ---
    # Sort by overall_score descending; break ties with attention_potential
    def sort_key(item: tuple[int, dict]) -> tuple[float, float]:
        _, s = item
        return (s.get("overall_score", 0), s.get("attention_potential", 0))

    sorted_candidates = sorted(scored.items(), key=sort_key, reverse=True)
    selected = sorted_candidates[:MAX_TOPICS]

    logger.info(
        "[topic-scorer] Selected top %d of %d candidates (MAX_TOPICS=%d)",
        len(selected),
        len(sorted_candidates),
        MAX_TOPICS,
    )
    for cid, scores in selected:
        logger.info(
            "  id=%4d  overall=%.3f  attn=%.3f  %s",
            cid,
            scores.get("overall_score", 0),
            scores.get("attention_potential", 0),
            scores.get("justification", "")[:100],
        )

    # --- Update DB ---
    for cid, scores in selected:
        overall = scores.get("overall_score", 0.5)
        cursor.execute(
            """UPDATE topics
               SET status = 'selected',
                   topic_score = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (overall, cid),
        )

        # Seed the analytics table with the predicted score for the learning loop
        cursor.execute(
            """INSERT INTO analytics (topic_id, predicted_score, fetched_at)
               VALUES (?, ?, datetime('now'))""",
            (cid, overall),
        )

    conn.commit()
    conn.close()

    # Log a structured selection summary
    summary = {
        "model": MODEL,
        "top_n": TOP_N,
        "candidates_scored": len(candidates),
        "selected": [
            {"id": cid, "overall_score": s.get("overall_score")}
            for cid, s in selected
        ],
    }
    logger.info("Selection complete: %s", json.dumps(summary))


def main():
    try:
        logger.info("Starting topic-scorer run ...")
        run()
        logger.info("topic-scorer complete.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
