"""Locates bundled resources (icons, ...) whether running from source or as
a PyInstaller-frozen executable — the spec's `datas` entry mirrors this
package's own `assets/` folder into the frozen bundle at the same relative
path, so both cases resolve the same way relative to this module.
"""

import os
import sys


def resource_path(*parts):
    if hasattr(sys, "_MEIPASS"):
        base = os.path.join(sys._MEIPASS, "kitsu_standalone_app")
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)
