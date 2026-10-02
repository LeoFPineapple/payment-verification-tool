"""Runtime bootstrap: make the vendored offline OCR packages importable.

On a normal machine ``pip install -r requirements.txt`` puts RapidOCR into the
interpreter's site-packages and this module does nothing.

In the locked-down environment this project was developed in, a *Deny* ACE on the
workspace root prevents ``pip`` from deleting its temporary ``*.whl.metadata``, so
``pip install`` cannot complete.  ``tools/vendor_ocr.py`` works around that by
downloading wheels and unzipping them into ``.pylibs/``; this module then puts that
directory on ``sys.path``.

Keeping the workaround here means the rest of the codebase, the GUI and the tests
all use plain ``import rapidocr_onnxruntime`` as if it had been pip-installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_VENDOR_DIR = Path(__file__).resolve().parents[2] / ".pylibs"

_bootstrapped = False


def vendor_dir() -> Path:
    """Location of the vendored wheels, whether or not it exists."""
    return _VENDOR_DIR


def bootstrap() -> bool:
    """Add the vendor directory to ``sys.path``.

    Returns:
        True when a vendor directory was found and added, False when the normal
        site-packages installation is being used.
    """
    global _bootstrapped
    if _bootstrapped:
        return _VENDOR_DIR.is_dir()
    _bootstrapped = True

    if _VENDOR_DIR.is_dir():
        path = str(_VENDOR_DIR)
        if path not in sys.path:
            # Prepended so a vendored package wins over a conflicting system one.
            sys.path.insert(0, path)
        return True
    return False
