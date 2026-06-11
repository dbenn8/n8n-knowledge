"""Pytest configuration for the hooks/lib Python unit suite.

Adds the plugin's ``hooks/lib`` directory to ``sys.path`` so the modules under
test (which import each other by bare module name, e.g. ``import plugin_config``)
can be imported directly.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "hooks", "lib"))

if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
