"""Launcher for the Streamlit GUI.

Equivalent to ``streamlit run app.py`` but runnable as a console script
(``pavt-gui``) and from anywhere::

    python -m pavt.launcher
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_path() -> Path:
    """Locate ``app.py`` in the project root."""
    return Path(__file__).resolve().parents[2] / "app.py"


def main(argv: list[str] | None = None) -> int:
    from streamlit.web import cli as stcli

    script = app_path()
    if not script.is_file():
        print(f"Could not find the GUI entry point at {script}", file=sys.stderr)
        return 1

    sys.argv = ["streamlit", "run", str(script), *list(argv or sys.argv[1:])]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
