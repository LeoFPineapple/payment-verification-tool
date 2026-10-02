"""Tests for the Payment Request Summary reader."""

from __future__ import annotations

from decimal import Decimal

import openpyxl
import pytest

from pavt.summary_reader import read_summary


def write_summary(path, rows, sheet_title="Sheet1"):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    return path


class TestReadSample:
    def test_reads_the_supplied_summary(self, sample_summary):
        result = read_summary(sample_summary)
        assert result.warnings == []
        assert len(result.requests) == 3

        first = result.requests[0]
        assert first.request_no == "P000001"
        assert first.payee_name == "ABC Medical Centre Limited"
        assert first.bank_code == "004"
        assert first.bank_account == "123456789"
        assert first.amount == Decimal("15000")

    def test_leading_zeros_survive_numeric_cells(self, sample_summary):
        """Bank code 004 is text-like; a numeric cell must still read as '004'."""
        result = read_summary(sample_summary)
        assert [r.bank_code for r in result.requests] == ["004", "004", "024"]

    def test_header_map_is_reported(self, sample_summary):
        result = read_summary(sample_summary)
        assert result.header_map["payment_amount"] == "Payment Amount (HKD)"


class TestHeaderMatching:
    def test_alternative_english_captions(self, tmp_path):
        path = write_summary(
            tmp_path / "s.xlsx",
            [
                ["Request No", "Beneficiary", "Bank Code", "Account No", "Description", "Amount"],
                ["P1", "ABC Ltd", "004", "123", "Fee", 100],
            ],
        )
        result = read_summary(path)
        assert result.requests[0].payee_name == "ABC Ltd"
        assert result.requests[0].amount == Decimal("100")

    def test_chinese_captions(self, tmp_path):
        path = write_summary(
            tmp_path / "s.xlsx",
            [
                ["付款請求編號", "收款人", "銀行編號", "銀行賬戶號碼", "摘要", "付款金額"],
                ["P1", "ABC 有限公司", "004", "123456", "服務費", 2500],
            ],
        )
        result = read_summary(path)
        assert len(result.requests) == 1
        assert result.requests[0].payee_name == "ABC 有限公司"
        assert result.requests[0].amount == Decimal("2500")

    def test_header_below_a_title_row(self, tmp_path):
        path = write_summary(
            tmp_path / "s.xlsx",
            [
                ["Payment Request Summary", None, None, None, None, None],
                [None, None, None, None, None, None],
                ["Payment Request No", "Payee Name", "Bank Code",
                 "Bank Account Number", "Particulars", "Payment Amount (HKD)"],
                ["P000009", "ABC Ltd", "004", "123456789", "Fee", 15000],
            ],
        )
        result = read_summary(path)
        assert len(result.requests) == 1
        assert result.requests[0].request_no == "P000009"


class TestRobustness:
    def test_missing_header_is_reported(self, tmp_path):
        path = write_summary(tmp_path / "s.xlsx", [["nonsense", "columns"], ["a", "b"]])
        result = read_summary(path)
        assert result.requests == []
        assert any("header row" in w for w in result.warnings)

    def test_rows_without_a_payee_are_skipped_with_a_reason(self, tmp_path):
        path = write_summary(
            tmp_path / "s.xlsx",
            [
                ["Payment Request No", "Payee Name", "Bank Code",
                 "Bank Account Number", "Particulars", "Payment Amount (HKD)"],
                ["P1", None, "004", "123", "Fee", 100],
                ["P2", "XYZ Ltd", "004", "456", "Fee", 200],
            ],
        )
        result = read_summary(path)
        assert len(result.requests) == 1
        assert any("Row 2" in row for row in result.skipped_rows)

    def test_rows_without_an_amount_are_skipped_with_a_reason(self, tmp_path):
        path = write_summary(
            tmp_path / "s.xlsx",
            [
                ["Payment Request No", "Payee Name", "Bank Code",
                 "Bank Account Number", "Particulars", "Payment Amount (HKD)"],
                ["P1", "ABC Ltd", "004", "123", "Fee", "not a number"],
                ["P2", "XYZ Ltd", "004", "456", "Fee", 200],
            ],
        )
        result = read_summary(path)
        assert len(result.requests) == 1
        assert any("Row 2" in row for row in result.skipped_rows)

    def test_blank_rows_are_ignored_silently(self, tmp_path):
        path = write_summary(
            tmp_path / "s.xlsx",
            [
                ["Payment Request No", "Payee Name", "Bank Code",
                 "Bank Account Number", "Particulars", "Payment Amount (HKD)"],
                ["P1", "ABC Ltd", "004", "123", "Fee", 100],
                [None, None, None, None, None, None],
            ],
        )
        result = read_summary(path)
        assert len(result.requests) == 1
        assert result.skipped_rows == []

    def test_empty_summary_is_reported(self, tmp_path):
        path = write_summary(
            tmp_path / "s.xlsx",
            [[
                "Payment Request No", "Payee Name", "Bank Code",
                "Bank Account Number", "Particulars", "Payment Amount (HKD)",
            ]],
        )
        result = read_summary(path)
        assert result.requests == []
        assert any("No usable payment request rows" in w for w in result.warnings)

    def test_request_no_defaults_when_absent(self, tmp_path):
        path = write_summary(
            tmp_path / "s.xlsx",
            [
                ["Payee Name", "Bank Code", "Bank Account Number", "Particulars", "Payment Amount"],
                ["ABC Ltd", "004", "123", "Fee", 100],
            ],
        )
        result = read_summary(path)
        assert result.requests[0].request_no == "ROW2"
