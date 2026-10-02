"""Test configuration and shared fixtures.

The sample files supplied with the task double as golden inputs, so the fixtures
locate them in the repository root and skip the OCR-dependent tests when no engine
is available instead of failing.

Running the suite::

    pytest                      # after pip install -r requirements.txt
    python -B -m pytest         # when using vendored wheels in .pylibs/
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(SRC), str(ROOT / ".pylibs")):
    if Path(path).is_dir() and path not in sys.path:
        sys.path.insert(0, path)

import pytest  # noqa: E402

from pavt.bootstrap import bootstrap  # noqa: E402

bootstrap()  # make vendored OCR wheels importable when present

SUMMARY_FILE = ROOT / "Payment Request Summary_Sample.xlsx"
INVOICE_FILES = (ROOT / "P0001.pdf", ROOT / "P0002.pdf", ROOT / "P0003.pdf")


@pytest.fixture(scope="session")
def sample_summary() -> Path:
    if not SUMMARY_FILE.exists():
        pytest.skip("sample Payment Request Summary is not present")
    return SUMMARY_FILE


@pytest.fixture(scope="session")
def sample_invoices() -> tuple[Path, ...]:
    missing = [p for p in INVOICE_FILES if not p.exists()]
    if missing:
        pytest.skip(f"sample invoice(s) missing: {missing}")
    return INVOICE_FILES


@pytest.fixture(scope="session")
def ocr_available() -> bool:
    try:
        from pavt.extraction import RapidOcrEngine

        RapidOcrEngine()
    except Exception:  # noqa: BLE001 - any failure means "not usable here"
        return False
    return True


@pytest.fixture(scope="session")
def ocr_engine(ocr_available):
    if not ocr_available:
        pytest.skip("offline OCR engine is not available in this environment")
    from pavt.extraction import RapidOcrEngine

    return RapidOcrEngine()
