"""End-to-end tests over the supplied sample files.

This is the acceptance test for the task: given the Payment Request Summary and
the three invoices shipped with the exercise, the tool must

* verify P000001 (15,000.00) and P000002 (5,800.00),
* detect that P000003 requests 125,000.00 against a 120,000.00 invoice, and
* emit a bank file containing only the two verified payments.
"""

from __future__ import annotations

import io
from decimal import Decimal

import openpyxl
import pytest

from pavt.models import MatchStatus, SourceKind
from pavt.pipeline import run_verification


def sheet_rows(content: bytes, title: str | None = None):
    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    sheet = workbook[title] if title else workbook.worksheets[0]
    return [row for row in sheet.iter_rows(values_only=True)]


class TestSampleBatchWithoutOcr:
    """Runs on the text-layer invoice only, so it needs no OCR models."""

    def test_p0003_is_held_back(self, sample_summary, sample_invoices):
        p0003 = [p for p in sample_invoices if p.name == "P0003.pdf"]
        report = run_verification(sample_summary, p0003)

        by_no = {r.request.request_no: r for r in report.results}
        assert set(by_no) == {"P000001", "P000002", "P000003"}

        result = by_no["P000003"]
        assert result.request.amount == Decimal("125000")
        assert result.invoice is not None
        assert result.invoice.source_kind is SourceKind.TEXT_LAYER
        assert result.invoice_amount == Decimal("120000.00")
        assert result.status is MatchStatus.AMOUNT_MISMATCH
        assert result.difference == Decimal("5000.00")
        assert not result.included_in_bank_file

        # P0001/P0002 were never supplied, so they must be reported as missing
        # documents -- not paired with P0003 just because amounts could coincide.
        for request_no in ("P000001", "P000002"):
            assert by_no[request_no].status is MatchStatus.NO_INVOICE
            assert by_no[request_no].invoice is None

    def test_bank_file_is_empty_when_nothing_verifies(self, sample_summary, sample_invoices):
        p0003 = [p for p in sample_invoices if p.name == "P0003.pdf"]
        report = run_verification(sample_summary, p0003)
        assert report.bank_file.row_count == 0
        assert report.total_payable == Decimal("0.00")


class TestSampleBatchEndToEnd:
    """The full acceptance run, including OCR of the two scanned invoices."""

    def test_verdicts(self, sample_summary, sample_invoices, ocr_engine):
        report = run_verification(sample_summary, sample_invoices, ocr_engine=ocr_engine)
        by_no = {r.request.request_no: r for r in report.results}

        assert by_no["P000001"].status is MatchStatus.MATCHED
        assert by_no["P000001"].invoice_amount == Decimal("15000.00")
        assert by_no["P000001"].invoice.source_kind is SourceKind.OCR_LOCAL

        assert by_no["P000002"].status is MatchStatus.MATCHED
        assert by_no["P000002"].invoice_amount == Decimal("5800.00")
        assert by_no["P000002"].invoice.source_kind is SourceKind.OCR_LOCAL

        assert by_no["P000003"].status is MatchStatus.AMOUNT_MISMATCH
        assert by_no["P000003"].invoice_amount == Decimal("120000.00")
        assert by_no["P000003"].invoice.source_kind is SourceKind.TEXT_LAYER

    def test_bank_file_contains_only_the_verified_payments(
        self, sample_summary, sample_invoices, ocr_engine
    ):
        report = run_verification(sample_summary, sample_invoices, ocr_engine=ocr_engine)

        assert report.bank_file.row_count == 2
        assert report.total_payable == Decimal("20800.00")

        rows = sheet_rows(report.bank_file.content)
        assert rows[0] == (
            "Payment Request No",
            "Payee Name",
            "Bank Code and Account Number",
            "Payment Amount (HKD)",
        )
        assert rows[1] == (
            "P000001",
            "ABC Medical Centre Limited",
            "004-123456789",
            15000,
        )
        assert rows[2] == (
            "P000002",
            "XYZ Office Supplies Limited",
            "004-456789123",
            5800,
        )
        # The mismatched request must not appear anywhere in the workbook.
        flat = [str(value) for row in rows for value in row]
        assert not any("Healthy Imaging" in value for value in flat)

    def test_audit_report_records_the_hold(
        self, sample_summary, sample_invoices, ocr_engine
    ):
        report = run_verification(sample_summary, sample_invoices, ocr_engine=ocr_engine)
        rows = sheet_rows(report.audit_report.content)
        statuses = {row[0]: row[7] for row in rows[1:]}
        assert statuses["P000001"] == "Matched"
        assert statuses["P000002"] == "Matched"
        assert statuses["P000003"] == "Amount mismatch"

    def test_force_ocr_reroutes_the_digital_invoice(
        self, sample_summary, sample_invoices, ocr_engine
    ):
        report = run_verification(
            sample_summary, sample_invoices, force_ocr=True, ocr_engine=ocr_engine
        )
        by_no = {r.request.request_no: r for r in report.results}
        # P0003.pdf goes through OCR, and the verdict must stay the same.
        assert by_no["P000003"].invoice.source_kind is SourceKind.OCR_LOCAL
        assert by_no["P000003"].status is MatchStatus.AMOUNT_MISMATCH
        assert by_no["P000003"].invoice_amount == Decimal("120000.00")

    def test_verify_all_three_pass_when_the_summary_is_corrected(
        self, sample_invoices, ocr_engine, tmp_path
    ):
        """Raising nothing but the expected amount lets all three through.

        Also demonstrates that a summary supplied from memory (as the GUI does)
        works the same way as one read from disk.
        """
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(
            ["Payment Request No", "Payee Name", "Bank Code",
             "Bank Account Number", "Particulars", "Payment Amount (HKD)"]
        )
        sheet.append(["P000001", "ABC Medical Centre Limited", "004", "123456789", "Clinic Service Fee", 15000])
        sheet.append(["P000002", "XYZ Office Supplies Limited", "004", "456789123", "Office Stationery", 5800])
        sheet.append(["P000003", "Healthy Imaging Centre Limited", "024", "987654321", "Imaging Service Fee", 120000])
        buffer = io.BytesIO()
        workbook.save(buffer)

        report = run_verification(
            ("corrected.xlsx", buffer.getvalue()), sample_invoices, ocr_engine=ocr_engine
        )
        assert [r.status for r in report.results] == [MatchStatus.MATCHED] * 3
        assert report.bank_file.row_count == 3
        assert report.total_payable == Decimal("140800.00")


class TestInputHandling:
    def test_missing_summary_columns_are_reported(self, sample_invoices, ocr_engine, tmp_path):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Payee Name", "Payment Amount"])
        sheet.append(["ABC Medical Centre Limited", 15000])
        buffer = io.BytesIO()
        workbook.save(buffer)

        report = run_verification(("thin.xlsx", buffer.getvalue()), sample_invoices[:1], ocr_engine=ocr_engine)
        # Payee + amount are enough to run; specifics it cannot verify are simply absent.
        assert any("missing expected column" in w for w in report.warnings)

    def test_unsupported_summary_is_reported(self, sample_invoices):
        report = run_verification(object(), sample_invoices)
        assert report.results == []
        assert any("Unsupported summary input" in w for w in report.warnings)

    def test_no_invoices_available(self, sample_summary):
        report = run_verification(sample_summary, [])
        assert len(report.results) == 3
        assert all(r.status is MatchStatus.NO_INVOICE for r in report.results)
        assert report.bank_file.row_count == 0
