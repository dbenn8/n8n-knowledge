#!/usr/bin/env python3
"""Resolve whether the plugin should use the local or cloud validator."""

from __future__ import annotations

import json
import os
import sys

from plugin_config import resolve_validator_target


def main() -> None:
    project_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    mode_override = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(resolve_validator_target(project_dir, mode_override)))


if __name__ == "__main__":
    main()
