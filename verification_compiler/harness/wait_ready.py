"""Exit 0 once the service answers any HTTP request, 1 on timeout."""
import sys
import time
import urllib.error
import urllib.request

base_url, timeout = sys.argv[1], float(sys.argv[2])
deadline = time.monotonic() + timeout
last_error = None
while time.monotonic() < deadline:
    try:
        urllib.request.urlopen(base_url.rstrip("/") + "/", timeout=2)
        sys.exit(0)
    except urllib.error.HTTPError:
        sys.exit(0)  # any HTTP status means the server is up
    except Exception as err:  # noqa: BLE001 - connection refused, DNS not ready, ...
        last_error = err
        time.sleep(0.5)
print(f"service not ready after {timeout}s: {last_error!r}")
sys.exit(1)
