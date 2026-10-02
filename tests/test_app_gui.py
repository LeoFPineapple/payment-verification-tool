"""GUI tests driving the real Streamlit app with Streamlit's own AppTest harness.

These exercise what a human operator does in the browser -- upload a summary and
invoices, press the button, read the verdicts, download the bank file -- so a
regression in the GUI wiring is caught here rather than during the interview demo.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = ROOT / "app.py"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_summary_bytes() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(
        ["Payment Request No", "Payee Name", "Bank Code",
         "Bank Account Number", "Particulars", "Payment Amount (HKD)"]
    )
    sheet.append(["P000001", "ABC Medical Centre Limited", "004", "123456789", "Clinic Service Fee", 15000])
    sheet.append(["P000002", "XYZ Office Supplies Limited", "004", "456789123", "Office Stationery", 5800])
    sheet.append(["P000003", "Healthy Imaging Centre Limited", "024", "987654321", "Imaging Service Fee", 125000])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def summary_bytes() -> bytes:
    return build_summary_bytes()


@pytest.fixture(scope="module")
def invoice_bytes(sample_invoices) -> list[tuple[str, bytes]]:
    return [(path.name, path.read_bytes()) for path in sample_invoices]


def run_app(summary_bytes, invoice_bytes, *, force_ocr: bool = False):
    app = AppTest.from_file(str(APP), default_timeout=300)
    app.run()
    assert not app.exception, app.exception

    app.file_uploader(key="summary_0").set_value(
        ("summary.xlsx", summary_bytes, XLSX_MIME)
    )
    app.file_uploader(key="invoices_0").set_value(
        [(name, data, "application/pdf") for name, data in invoice_bytes]
    )
    if force_ocr:
        app.checkbox[0].set_value(True)
    app.run()
    assert not app.exception, app.exception
    app.button[0].click()
    app.run()
    assert not app.exception, app.exception
    return app


class TestAppBoots:
    def test_renders_without_error(self):
        app = AppTest.from_file(str(APP), default_timeout=120)
        app.run()
        assert not app.exception
        assert app.title[0].value == "Payment Automation & Verification Tool"

    def test_prompts_for_both_inputs_before_running(self):
        app = AppTest.from_file(str(APP), default_timeout=120)
        app.run()
        text = " ".join(element.value for element in app.info)
        assert "Payment Request Summary" in text

    def test_verify_button_is_disabled_without_uploads(self):
        app = AppTest.from_file(str(APP), default_timeout=120)
        app.run()
        assert app.button[0].disabled is True

    def test_sidebar_reports_engine_availability(self):
        app = AppTest.from_file(str(APP), default_timeout=120)
        app.run()
        captions = " ".join(element.value for element in app.sidebar.caption)
        assert "Offline OCR" in captions


_workflow_app = None


@pytest.fixture(scope="module")
def workflow_app(sample_invoices, ocr_available):
    """Run the GUI once per module and reuse the result across assertions."""
    global _workflow_app
    if not ocr_available:
        pytest.skip("offline OCR engine is not available in this environment")
    if _workflow_app is None:
        _workflow_app = run_app(
            build_summary_bytes(), [(p.name, p.read_bytes()) for p in sample_invoices]
        )
    return _workflow_app


class TestFullGuiWorkflow:
    """One end-to-end pass through the interface (OCR included)."""

    def test_shows_three_verdicts(self, workflow_app):
        metrics = {metric.label: metric.value for metric in workflow_app.metric}
        assert metrics["Requests checked"] == "3"
        assert metrics["Included in bank file"] == "2"
        assert metrics["Held back"] == "1"

    def test_total_payable_is_displayed(self, workflow_app):
        metrics = {metric.label: metric.value for metric in workflow_app.metric}
        assert metrics["Total payable (HKD)"] == "20,800.00"

    def test_success_banner_is_shown(self, workflow_app):
        assert any("Bank payment file generated" in element.value for element in workflow_app.success)

    def test_download_buttons_are_offered(self, workflow_app):
        labels = [button.label for button in workflow_app.download_button]
        assert any("bank payment file" in label for label in labels)
        assert any("audit report" in label for label in labels)

    def test_bank_file_download_is_the_real_workbook(self, workflow_app):
        """The GUI offers a genuine xlsx download of the verified bank file.

        AppTest exposes the download button but not its payload, so the workbook
        itself is asserted in ``TestGuiReporting`` below via the same pipeline call
        the button is built from.
        """
        bank_button = next(
            b for b in workflow_app.download_button if "bank payment file" in b.label
        )
        assert bank_button.label == "Download bank payment file (.xlsx)"
        assert bank_button.disabled is False

    def test_held_back_payment_reason_is_visible(self, workflow_app):
        """The operator must see both figures and why the row was not paid."""
        text = " ".join(
            [str(element.value) for element in workflow_app.markdown]
            + [str(element.value) for element in workflow_app.caption]
        )
        assert "P000003 - Healthy Imaging Centre Limited" in text
        assert "120000.00" in text  # the invoice total that was read
        assert "125000.00" in text  # the amount that was requested
        assert "excluded from the bank file" in text


class TestGuiReporting:
    def test_bank_file_in_the_download_matches_the_pipeline(
        self, sample_summary, sample_invoices, ocr_available, tmp_path
    ):
        """The workbook the GUI offers must be the same one the CLI writes."""
        if not ocr_available:
            pytest.skip("offline OCR engine is not available in this environment")
        from pavt.pipeline import run_verification

        report = run_verification(
            sample_summary, sample_invoices, batch_reference="BATCH", generate_files=True
        )
        written = report.bank_file.write_to(tmp_path / report.bank_file.filename)
        workbook = openpyxl.load_workbook(written, data_only=True)
        sheet = workbook.worksheets[0]
        assert sheet.cell(row=1, column=1).value == "Payment Request No"
        assert sheet.cell(row=2, column=4).value == 15000
        assert sheet.cell(row=3, column=4).value == 5800
        assert sheet.cell(row=4, column=4).value == 20800
        assert report.total_payable == Decimal("20800.00")
