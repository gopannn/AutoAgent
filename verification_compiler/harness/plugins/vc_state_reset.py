"""pytest plugin (oracle only): empties the backing Redis before every test function.

Each test starts from a known-empty store, so a test cannot pass or fail because of what an
earlier test left behind. Only the oracle holds the admin credentials; the service's own Redis
user cannot flush. A failed reset is recorded in /out/state_reset.error and fails the run as an
infrastructure error, never as the service's fault.
"""
import os
import socket

import pytest

# Taken out of the environment before any test module is imported.
_ADDR = os.environ.pop("VC_REDIS_ADDR", "")
_PASSWORD = os.environ.pop("VC_REDIS_PASSWORD", "")
_ERROR_FILE = "/out/state_reset.error"


def _encode(*parts: str) -> bytes:
    out = [f"*{len(parts)}\r\n".encode()]
    for part in parts:
        data = part.encode()
        out.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(out)


def reset(addr: str = _ADDR, password: str = _PASSWORD) -> None:
    host, _, port = addr.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        stream = sock.makefile("rwb")
        for command in (("AUTH", "oracle", password), ("FLUSHALL", "SYNC")):
            stream.write(_encode(*command))
            stream.flush()
            reply = stream.readline()
            if not reply.startswith(b"+OK"):
                raise RuntimeError(f"{command[0]} failed: {reply[:200]!r}")


@pytest.fixture(autouse=True)
def _vc_reset_backing_state():
    try:
        reset()
    except Exception as err:  # noqa: BLE001 - any failure here is the harness's, and is reported as such
        with open(_ERROR_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{type(err).__name__}: {err}\n")
        raise
    yield
