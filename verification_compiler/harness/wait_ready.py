"""Exit 0 once every service replica answers any HTTP request, 1 on timeout.

argv[1] is one base URL or a comma-separated list (one per replica); all share one deadline.
"""
import sys
import time
import urllib.error
import urllib.request

urls, timeout = [u for u in sys.argv[1].split(",") if u], float(sys.argv[2])
deadline = time.monotonic() + timeout


def ready(base_url: str) -> bool:
    try:
        urllib.request.urlopen(base_url.rstrip("/") + "/", timeout=2)
    except urllib.error.HTTPError:
        pass  # any HTTP status means the server is up
    return True


last_error = None
pending = list(urls)
while pending and time.monotonic() < deadline:
    try:
        ready(pending[0])
        pending.pop(0)
    except Exception as err:  # noqa: BLE001 - connection refused, DNS not ready, ...
        last_error = err
        time.sleep(0.5)
if pending:
    print(f"service not ready after {timeout}s at {pending[0]}: {last_error!r}")
    sys.exit(1)
sys.exit(0)
