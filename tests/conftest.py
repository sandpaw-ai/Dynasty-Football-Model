"""Repo-local pytest setup.

Ensures the v2.3.5 fork's ``src`` directory is imported in preference
to any system-wide editable install of ``dynasty-model`` (there is
typically a .pth file pointing at ``~/work/dynasty-football-model/src``
which would otherwise win and cause our tests to assert against the
wrong module). We additionally evict any already-loaded ``dynasty.*``
modules so an earlier import via the system path does not poison
subsequent imports.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))

# Put our src AHEAD of every other entry on sys.path.
if _REPO_SRC in sys.path:
    sys.path.remove(_REPO_SRC)
sys.path.insert(0, _REPO_SRC)

# If dynasty was already loaded from a different src, evict it so the
# next import re-resolves to our path.
_dynasty_loaded_from = (
    getattr(sys.modules.get("dynasty"), "__file__", None)
    if "dynasty" in sys.modules
    else None
)
if _dynasty_loaded_from and not _dynasty_loaded_from.startswith(_REPO_SRC):
    for name in list(sys.modules):
        if name == "dynasty" or name.startswith("dynasty."):
            del sys.modules[name]
