# History Rhymes

An attention-intelligence content pipeline that produces original YouTube Shorts pairing historical "on this day" events with current events that echo them.

## Architecture

All components run as Docker containers orchestrated by `docker-compose.yml`, sharing a persistent SQLite database via a named volume.

```
[trend-scout] → [topic-scorer] → [research] → [scriptwriter] →
[visual-sourcing] + [voice-gen] → [assembly] → [review-ui] →
[publisher] → [analytics] → (feeds back into topic-scorer)
```

## Quick Start

```bash
# Build and run all services
docker compose up --build

# Run end-to-end test
./test_e2e.sh

# Review UI available at
# http://localhost:8000/health
```

## Service Overview

| Service | Description | Port |
|---------|-------------|------|
| db-init | One-shot: creates SQLite DB and runs schema migrations | — |
| trend-scout | Polls historical/trend sources for candidate pairings | — |
| topic-scorer | Scores and selects topics via Claude API | — |
| research | Pulls factual summaries with citations | — |
| scriptwriter | Synthesizes original narration scripts via Claude API | — |
| visual-sourcing | Matches script segments to public-domain/licensed visuals | — |
| voice-gen | Text-to-speech via licensed TTS API | — |
| assembly | Combines audio, visuals, captions via ffmpeg | — |
| review-ui | FastAPI dashboard for human review | 8000 |
| publisher | YouTube Data API upload | — |
| analytics | Pulls and logs performance metrics | — |

## Shared Dependencies

Individual services specify their dependencies in `requirements.txt` files within their service directories. A root `requirements.txt` is provided for development convenience.

## Database

SQLite at `/data/history_rhymes.db` inside containers (shared via `db-data` named volume).

## Environment Variables

Configure via `.env` file at repo root. See each service's code for specific variables — all have sensible defaults for local development.

## License

Proprietary — all rights reserved.
