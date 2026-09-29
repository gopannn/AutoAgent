"""Access to the vendored reasoning engines (third_party/ctd, third_party/epistemic_toolkit).

Installed packages are preferred; a repository checkout falls back to the vendored source
trees. Either way a missing engine is an infrastructure fault: the governed layers fail
closed rather than being silently skipped.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

from .errors import InfrastructureError

_THIRD_PARTY = Path(__file__).resolve().parents[1] / "third_party"
_SOURCES = {"ctd": _THIRD_PARTY / "ctd" / "src", "epistemic_toolkit": _THIRD_PARTY / "epistemic_toolkit" / "src"}


def load(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ImportError:
        src = _SOURCES[name]
        if src.is_dir() and str(src) not in sys.path:
            sys.path.append(str(src))
        try:
            return importlib.import_module(name)
        except ImportError as err:
            raise InfrastructureError(
                f"reasoning engine '{name}' is unavailable ({err}); install it with pip install ./third_party/"
                f"{'ctd' if name == 'ctd' else 'epistemic_toolkit'}"
            ) from err


def ctd_module(path: str) -> ModuleType:
    load("ctd")
    return importlib.import_module(f"ctd.{path}")


def toolkit() -> ModuleType:
    return load("epistemic_toolkit")
