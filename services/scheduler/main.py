import subprocess
import sys
import os
import time

SERVICES = [
    ("trend-scout", "candidates", "candidate"),
    ("topic-scorer", "scored", "selected"),
    ("research", "facts", "sources"),
    ("scriptwriter", "scripts", "draft"),
    ("visual-sourcing", "images", "visual assets"),
    ("voice-gen", "audio", "audio files"),
    ("assembly", "videos", "draft videos"),
    ("publisher", "published", "videos published"),
    ("analytics", "analytics", "stats pulled"),
]

def run_stage(service_name, verb, noun):
    print(f"\n[scheduler] === Stage: {service_name} ===")
    try:
        result = subprocess.run(
            ["docker", "compose", "run", "--rm", "-T", service_name],
            capture_output=True, text=True, timeout=300,
            cwd="/repo"
        )
        # Print service output
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        
        if result.returncode == 0:
            # Try to parse meaningful output for a summary
            summary = extract_summary(service_name, result.stdout)
            print(f"[scheduler] {service_name}: SUCCESS — {summary}")
            return True
        else:
            print(f"[scheduler] {service_name}: FAILED (exit code {result.returncode})")
            return False
    except subprocess.TimeoutExpired:
        print(f"[scheduler] {service_name}: FAILED (timed out after 300s)")
        return False
    except Exception as e:
        print(f"[scheduler] {service_name}: FAILED ({e})")
        return False

def extract_summary(service, output):
    # Parse output for key metrics
    output_lower = output.lower()
    if service == "trend-scout":
        # Count candidate insertions
        import re
        matches = re.findall(r'inserted.*?candidate|wrote.*?candidate|candidate.*?inserted', output_lower)
        if matches:
            return f"discovered candidates (see logs for count)"
        return "candidates written to DB"
    elif service == "topic-scorer":
        if "selected" in output_lower:
            return "scored and selected topics (see logs)"
        return "scoring complete"
    elif service == "research":
        if "facts" in output_lower or "sources" in output_lower:
            return "facts pulled with citations"
        return "research complete"
    elif service == "scriptwriter":
        if "scripts" in output_lower or "draft" in output_lower:
            return "scripts synthesized"
        return "scriptwriting complete"
    elif service == "assembly":
        if "video" in output_lower or "rendered" in output_lower:
            return "videos rendered"
        return "assembly complete"
    elif service == "publisher":
        if "published" in output_lower:
            return "videos published to YouTube"
        if "no approved" in output_lower or "skipping" in output_lower:
            return "SKIPPED — no approved videos"
        return "publishing complete"
    elif service == "analytics":
        if "no published" in output_lower or "skipping" in output_lower:
            return "SKIPPED — no published videos"
        return "analytics pulled"
    else:
        return "completed"

def main():
    # Pre-flight checks
    if not os.path.exists("/var/run/docker.sock"):
        print("[scheduler] FATAL: Docker socket not mounted at /var/run/docker.sock")
        sys.exit(1)
    if not os.path.exists("/repo/docker-compose.yml"):
        print("[scheduler] FATAL: docker-compose.yml not found at /repo")
        sys.exit(1)
    
    print("[scheduler] Pipeline starting...")
    print("[scheduler] Review dashboard will be available at http://localhost:8000")
    
    succeeded = 0
    failed = 0
    skipped = 0
    
    for service, verb, noun in SERVICES:
        ok = run_stage(service, verb, noun)
        if ok:
            succeeded += 1
        else:
            failed += 1
    
    print("\n[scheduler] ==================================")
    print(f"[scheduler] Pipeline complete: {succeeded} succeeded, {failed} failed, {skipped} skipped")
    print(f"[scheduler] Review dashboard: http://localhost:8000")
    print("[scheduler] ==================================")

if __name__ == "__main__":
    main()
