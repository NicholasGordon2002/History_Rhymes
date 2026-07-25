"""
voice-gen: Text-to-speech narration generation for YouTube Shorts.

For each topic with status='draft' (scripts written but audio not yet generated):
  1. Pulls the script_text from the scripts table
  2. Generates MP3 audio via TTS engine
  3. Saves output to /data/audio/{topic_id}.mp3

TTS backends:
  - gTTS (default): Google Text-to-Speech, free, no API key required.
    Good for dev/testing. Architecturally swappable — see TTS_BACKEND config.
  - ElevenLabs / Amazon Polly: Production options, configurable via env vars.
    Swap by changing the generate_audio() backend selection below.

One-shot batch job: runs, generates audio, saves, exits.
"""

import logging
import os
import sqlite3
import sys

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
TTS_API_KEY = os.environ.get("TTS_API_KEY", "")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")

# TTS backend selection: "gtts", "elevenlabs", or "polly"
TTS_BACKEND = os.environ.get("TTS_BACKEND", "gtts")

# gTTS settings
GTTS_LANG = os.environ.get("GTTS_LANG", "en")
GTTS_TLD = os.environ.get("GTTS_TLD", "com")  # "co.uk", "com.au", etc. for accent

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[voice-gen] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("voice-gen")


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
    """Return all scripts whose topics have status='draft'."""
    cursor = conn.execute(
        """
        SELECT s.id AS script_id, s.topic_id, s.script_text,
               t.historical_event_title, t.modern_event_title
        FROM scripts s
        JOIN topics t ON s.topic_id = t.id
        WHERE t.status = 'draft'
        ORDER BY s.topic_id
        """
    )
    return cursor.fetchall()


def audio_file_exists(topic_id: int) -> bool:
    """Check if audio file already exists (idempotency check)."""
    audio_path = os.path.join(AUDIO_DIR, f"{topic_id}.mp3")
    return os.path.isfile(audio_path)


# ---------------------------------------------------------------------------
# TTS Backend: gTTS (Google Text-to-Speech)
# ---------------------------------------------------------------------------

def generate_audio_gtts(topic_id: int, script_text: str) -> str:
    """
    Generate MP3 audio using gTTS (free, no API key required).
    Returns the output file path.
    """
    from gtts import gTTS

    os.makedirs(AUDIO_DIR, exist_ok=True)
    audio_path = os.path.join(AUDIO_DIR, f"{topic_id}.mp3")

    logger.info(f"  Generating audio via gTTS (lang={GTTS_LANG}, tld={GTTS_TLD})")
    tts = gTTS(text=script_text, lang=GTTS_LANG, tld=GTTS_TLD, slow=False)
    tts.save(audio_path)

    file_size = os.path.getsize(audio_path)
    logger.info(f"  Audio saved: {audio_path} ({file_size} bytes)")

    return audio_path


# ---------------------------------------------------------------------------
# TTS Backend: ElevenLabs (placeholder — requires API key)
# ---------------------------------------------------------------------------

def generate_audio_elevenlabs(topic_id: int, script_text: str) -> str:
    """
    Generate MP3 audio using ElevenLabs API.
    Requires TTS_API_KEY env var set with a valid ElevenLabs API key.
    """
    import requests

    if not TTS_API_KEY:
        raise RuntimeError("TTS_API_KEY is required for ElevenLabs backend")

    os.makedirs(AUDIO_DIR, exist_ok=True)
    audio_path = os.path.join(AUDIO_DIR, f"{topic_id}.mp3")

    # ElevenLabs TTS v1 API — using a generic "Rachel" voice ID as default
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": TTS_API_KEY,
    }
    payload = {
        "text": script_text,
        "model_id": "eleven_monolingual_v1",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }

    logger.info(f"  Generating audio via ElevenLabs (voice_id={voice_id})")
    resp = requests.post(url, json=payload, headers=headers, timeout=60.0)
    resp.raise_for_status()

    with open(audio_path, "wb") as f:
        f.write(resp.content)

    file_size = os.path.getsize(audio_path)
    logger.info(f"  Audio saved: {audio_path} ({file_size} bytes)")

    return audio_path


# ---------------------------------------------------------------------------
# TTS Backend: Amazon Polly (placeholder — requires AWS credentials)
# ---------------------------------------------------------------------------

def generate_audio_polly(topic_id: int, script_text: str) -> str:
    """
    Generate MP3 audio using Amazon Polly.
    Requires AWS credentials configured (env vars or ~/.aws/credentials).
    """
    import boto3

    os.makedirs(AUDIO_DIR, exist_ok=True)
    audio_path = os.path.join(AUDIO_DIR, f"{topic_id}.mp3")

    voice_id = os.environ.get("POLLY_VOICE_ID", "Joanna")
    engine = os.environ.get("POLLY_ENGINE", "neural")

    logger.info(f"  Generating audio via Amazon Polly (voice={voice_id}, engine={engine})")
    polly = boto3.client("polly")
    resp = polly.synthesize_speech(
        Text=script_text,
        OutputFormat="mp3",
        VoiceId=voice_id,
        Engine=engine,
    )

    with open(audio_path, "wb") as f:
        f.write(resp["AudioStream"].read())

    file_size = os.path.getsize(audio_path)
    logger.info(f"  Audio saved: {audio_path} ({file_size} bytes)")

    return audio_path


# ---------------------------------------------------------------------------
# Backend dispatcher
# ---------------------------------------------------------------------------

# Map backend names to generator functions
BACKENDS = {
    "gtts": generate_audio_gtts,
    "elevenlabs": generate_audio_elevenlabs,
    "polly": generate_audio_polly,
}


def generate_audio(topic_id: int, script_text: str) -> str:
    """Dispatch to the configured TTS backend."""
    backend_fn = BACKENDS.get(TTS_BACKEND)
    if backend_fn is None:
        raise ValueError(
            f"Unknown TTS_BACKEND '{TTS_BACKEND}'. "
            f"Valid options: {', '.join(BACKENDS.keys())}"
        )
    return backend_fn(topic_id, script_text)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    logger.info(f"Starting voice-gen run (backend={TTS_BACKEND})...")

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

        logger.info(f"Found {len(scripts)} script(s) to generate audio for.")

        generated = 0
        skipped = 0
        errors = 0

        for script in scripts:
            topic_id = script["topic_id"]
            script_id = script["script_id"]
            script_text = script["script_text"]

            title = script["historical_event_title"] or f"topic {topic_id}"
            logger.info(f"Topic {topic_id}: processing ({title})")

            # Idempotency: skip if audio already exists
            if audio_file_exists(topic_id):
                logger.info(f"  Audio already exists for topic {topic_id}, skipping")
                skipped += 1
                continue

            if not script_text or not script_text.strip():
                logger.warning(f"  Topic {topic_id}: empty script_text, skipping")
                skipped += 1
                continue

            try:
                generate_audio(topic_id, script_text)
                generated += 1
            except Exception as e:
                logger.error(f"  Topic {topic_id}: TTS generation failed: {e}")
                errors += 1

        logger.info(
            f"voice-gen run complete: {generated} generated, "
            f"{skipped} skipped, {errors} errors"
        )

        if errors > 0:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
