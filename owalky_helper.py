#!/usr/bin/python3
"""Owalky helper: the only executable the panel runs.

The panel invokes this as an argv array with the interpreter in isolated mode:

    /usr/bin/python3 -I <plugin dir>/owalky_helper.py status

``-I`` keeps ``PYTHONPATH``, the user site directory and the working directory off
``sys.path``, so the only ``bleak`` importable here is the one the system package
manager installed. It also drops this script's own directory, which is why the
package beside it is added explicitly.

The same subcommands work by hand:

    owalky_helper.py status
    owalky_helper.py --mac AA:BB:CC:DD:EE:FF connect
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from owalky.cli import main_entry  # noqa: E402  (the package lives next to this file)

if __name__ == "__main__":
    sys.exit(main_entry())
