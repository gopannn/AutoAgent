"""Print the versions of the tools actually present in this image, as JSON."""
import json
import subprocess
import sys
from importlib import metadata

versions = {"python": sys.version.split()[0]}
for dist in ("ruff", "pytest", "semgrep", "httpx", "PyJWT"):
    try:
        versions[dist.lower()] = metadata.version(dist)
    except metadata.PackageNotFoundError:
        pass
try:
    out = subprocess.run(["pyright", "--version"], capture_output=True, text=True, timeout=60).stdout
    versions["pyright"] = out.split()[-1] if out.strip() else "unknown"
except (OSError, subprocess.SubprocessError):
    pass
print(json.dumps(versions, sort_keys=True))
