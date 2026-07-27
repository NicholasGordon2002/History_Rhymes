"""
scheduler: Pipeline orchestrator for History Rhymes.

Runs each pipeline stage sequentially via `docker compose run --rm`.
Parses output to extract metrics and prints clear summary lines.
Continues through failures so one broken stage doesn't block downstream visibility.
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timezone


# --- Configuration (from environment) ---

REPO_PATH    = os.environ.get("REPO_PATH", "/repo")
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", f"{REPO_PATH}/docker-compose.yml")
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "history-rhymes")
STAGE_TIMEOUT = int(os.environ.get("STAGE_TIMEOUT", "300"))  # seconds per stage

STAGES = [
    "trend-scout",
    "topic-scorer",
    "research",
    "scriptwriter",
    "visual-sourcing",
    "voice-gen",
    "assembly",
    "publisher",
    "analytics",
]


# --- Helpers ---

def run_stage(service_name: str) -> subprocess.CompletedProcess | None:
    """Run one pipeline stage via docker compose, return CompletedProcess or None on timeout."""
    cmd = [
        "docker", "compose",
        "-f", COMPOSE_FILE,
        "-p", COMPOSE_PROJECT,
        "run", "--rm", "-T",  # -T = no TTY allocation (clean subprocess output)
        service_name,
    ]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=STAGE_TIMEOUT,
            cwd=REPO_PATH,
        )
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        print(
            "[scheduler] FATAL: docker CLI not found. Is the Docker socket mounted?",
            file=sys.stderr,
        )
        sys.exit(1)


def extract_metrics(service_name: str, stdout: str) -> dict:
    """Pull structured metrics from a service's stdout (best-effort)."""
    m: dict = {}

    if service_name == "trend-scout":
        # "[trend-scout] Inserted test candidate topic (id=N)"
        matches = re.findall(r"Inserted test candidate topic \(id=\d+\)", stdout)
        m["candidates"] = len(matches)
        # Also count lines with "Inserted test candidate"
        if m["candidates"] == 0:
            m["candidates"] = len(re.findall(r"Inserted test candidate topic", stdout))

    elif service_name == "topic-scorer":
        # "[topic-scorer] Found N candidate(s)"
        match = re.search(r"Found (\d+) candidate", stdout)
        m["candidates_found"] = int(match.group(1)) if match else 0
        # "[topic-scorer] Selected topic id=N"
        selected = re.findall(r"Selected topic id=\d+", stdout)
        m["selected"] = len(selected)

    elif service_name == "research":
        # future: count facts / sources
        pass

    elif service_name == "scriptwriter":
        # future: count scripts / avg duration
        pass

    elif service_name == "visual-sourcing":
        # future: count matched images
        pass

    elif service_name == "voice-gen":
        # future: count generated audio files
        pass

    elif service_name == "assembly":
        # future: count rendered draft videos
        pass

    elif service_name == "publisher":
        # future: count published
        pass

    elif service_name == "analytics":
        # future: count videos with pulled stats
        pass

    return m


def format_summary(service_name: str, metrics: dict, returncode: int | str) -> str:
    """Build the canonical [scheduler] summary line for one stage."""

    if returncode == "TIMEOUT":
        return f"[scheduler] {service_name}: FAILED (timeout) — continuing with remaining stages"

    if returncode != 0:
        return (
            f"[scheduler] {service_name}: FAILED (exit code {returncode}) "
            f"— continuing with remaining stages"
        )

    # Success — build a human-readable line from known metrics
    if service_name == "trend-scout":
        n = metrics.get("candidates", "?")
        return f"[scheduler] trend-scout: SUCCESS — discovered {n} candidates"

    if service_name == "topic-scorer":
        found = metrics.get("candidates_found", "?")
        sel   = metrics.get("selected", "?")
        return f"[scheduler] topic-scorer: SUCCESS — scored {found} candidates, selected top {sel}"

    # Generic fallback for stubbed services
    labels = {
        "research":        "research: SUCCESS — DB connection verified",
        "scriptwriter":    "scriptwriter: SUCCESS — DB connection verified",
        "visual-sourcing": "visual-sourcing: SUCCESS — DB connection verified",
        "voice-gen":       "voice-gen: SUCCESS — DB connection verified",
        "assembly":        "assembly: SUCCESS — DB connection verified",
        "publisher":       "publisher: SUCCESS — DB connection verified",
        "analytics":       "analytics: SUCCESS — DB connection verified",
    }
    return f"[scheduler] {labels.get(service_name, f'{service_name}: SUCCESS')}"


# --- Main ---

def main() -> None:
    now_utc = datetime.now(timezone.utc).isoformat()
    print("[scheduler] ==================================")
    print("[scheduler] History Rhymes Pipeline Scheduler")
    print(f"[scheduler] Started: {now_utc}")
    print(f"[scheduler] Project: {COMPOSE_PROJECT}")
    print("[scheduler] ==================================")
    print()

    # --- Guards ---
    if not os.path.exists("/var/run/docker.sock"):
        print(
            "[scheduler] FATAL: Docker socket not mounted at /var/run/docker.sock",
            file=sys.stderr,
        )
        print(
            "[scheduler] Mount it with: -v /var/run/docker.sock:/var/run/docker.sock",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.path.isfile(COMPOSE_FILE):
        print(f"[scheduler] FATAL: Compose file not found: {COMPOSE_FILE}", file=sys.stderr)
        sys.exit(1)

    # --- Pipeline ---
    results: dict[str, dict] = {}
    total = len(STAGES)

    for i, service in enumerate(STAGES, 1):
        print(f"[scheduler] === Stage {i}/{total}: {service} ===")

        result = run_stage(service)

        if result is None:
            # Timeout
            print(format_summary(service, {}, "TIMEOUT"))
            results[service] = {"status": "failed", "reason": "timeout"}
        else:
            # Passthrough the service's own stdout so nothing is hidden
            if result.stdout:
                print(result.stdout.rstrip())
            if result.stderr:
                print(result.stderr.rstrip(), file=sys.stderr)

            returncode = result.returncode
            metrics = extract_metrics(service, result.stdout)
            summary = format_summary(service, metrics, returncode)
            print(summary)

            results[service] = {
                "status": "success" if returncode == 0 else "failed",
                "returncode": returncode,
                "metrics": metrics,
            }

        print()  # blank line between stages

    # --- Final summary ---
    succeeded = sum(1 for r in results.values() if r["status"] == "success")
    failed   = sum(1 for r in results.values() if r["status"] == "failed")
    skipped  = sum(1 for r in results.values() if r["status"] == "skipped")

    print("[scheduler] ==================================")
    print(f"[scheduler] Pipeline complete: {succeeded} succeeded, {failed} failed, {skipped} skipped")

    # Count draft videos ready for review (future: query DB)
    print("[scheduler] Videos ready for review: see review dashboard")
    print("[scheduler] Review dashboard: http://localhost:8000")
    print("[scheduler] ==================================")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
