"""Container health check: `python -m vibe_flipper.healthcheck` (exit 0 = healthy)."""
import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
