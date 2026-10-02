"""Unit tests for invoice parsing and the OCR reading-order reconstruction."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pavt.extraction import group_boxes_into_lines, looks_like_text_layer
from pavt.invoice_parser import (
    fold_text,
    guess_payee_from_header,
    parse_fields,
    parse_invoice_text,
)
from pavt.models import SourceKind

# Real OCR output from the supplied scans (whitespace preserved).
OCR_TEXT_P0001 = (
    "ABC Medical Centre Limited\n"
    "Payee Name  Bank Code  Bank Account Number\n"
    "ABC Medical Centre Limited  004  123456789\n"
    "Particulars  Amount (HKD)\n"
    "Clinic Service Fee  15,000.00\n"
    "TotalAmount:HKD15,000.00"
)

OCR_TEXT_P0002 = (
    "XYZ Office Supplies Limited\n"
    "發票（INVOICE）\n"
    "發票编号  INV101213\n"
    "發票日期  2026/09/24\n"
    "到期日  2026/10/24\n"
    "银行编號  004\n"
    "银行眼户號碼  456789123\n"
    "项目  金额(HKD)\n"
    "辨公室文具  5,800.00\n"
    "總計  5,800.00"
)

TEXT_LAYER_P0003 = (
    "Healthy Imaging Centre Limited\n"
    "TAX INVOICE\n"
    "Invoice No.\n140981024\n"
    "Invoice Date\n2026/09/08\n"
    "Due Date\n2026/11/08\n"
    "Bank Code\n024\n"
    "Bank Account Number\n987654321\n"
    "Particulars\nAmount (HKD)\n"
    "Imaging Service Fee\n120,000.00\n"
    "TOTAL\n120,000.00"
)


class TestGroupBoxesIntoLines:
    def test_sorts_boxes_into_visual_rows(self):
        # Deliberately out of order, with labels and values on separate y values.
        boxes = [
            (500.0, 520.0, 300.0, 400.0, "004"),
            (482.0, 502.0, 60.0, 200.0, "Bank Code"),
            (482.0, 502.0, 300.0, 460.0, "Payee Name"),
            (500.0, 520.0, 60.0, 280.0, "ABC Medical Centre Limited"),
        ]
        text = group_boxes_into_lines(boxes)
        lines = text.splitlines()
        assert lines[0].startswith("Bank Code")
        assert "Payee Name" in lines[0]
        assert lines[1].startswith("ABC Medical Centre Limited")
        assert "004" in lines[1]

    def test_close_boxes_are_joined_with_a_single_space(self):
        boxes = [
            (10.0, 30.0, 0.0, 40.0, "ABC"),
            (10.0, 30.0, 44.0, 90.0, "Medical"),
        ]
        assert group_boxes_into_lines(boxes) == "ABC Medical"

    def test_distant_boxes_are_separated_as_columns(self):
        boxes = [
            (10.0, 30.0, 0.0, 40.0, "Label"),
            (10.0, 30.0, 300.0, 360.0, "Value"),
        ]
        assert group_boxes_into_lines(boxes) == "Label  Value"

    def test_empty_input(self):
        assert group_boxes_into_lines([]) == ""


class TestLooksLikeTextLayer:
    def test_letterhead_only_is_not_a_text_layer(self):
        # P0001/P0002 carry only the supplier name; OCR is still required.
        assert not looks_like_text_layer("ABC Medical Centre Limited")

    def test_short_noise_is_not_a_text_layer(self):
        assert not looks_like_text_layer("   \n  \n")

    def test_full_invoice_text_is_a_text_layer(self):
        assert looks_like_text_layer(TEXT_LAYER_P0003)


class TestFoldText:
    def test_folds_full_width_and_variant_characters(self):
        assert fold_text("银行编號  ０１２") == "银行编号  012"
        assert fold_text("總計") == "总计"


class TestParseFields:
    def test_header_row_table_layout(self):
        fields = parse_fields(OCR_TEXT_P0001)
        assert fields["payee_name"] == "ABC Medical Centre Limited"
        assert fields["bank_code"] == "004"
        assert fields["bank_account"] == "123456789"

    def test_chinese_layout_with_wrapped_labels(self):
        fields = parse_fields(OCR_TEXT_P0002)
        assert fields["bank_code"] == "004"
        assert fields["bank_account"] == "456789123"
        assert fields["invoice_no"] == "INV101213"
        assert fields["invoice_date"] == "2026/09/24"

    def test_label_then_value_on_separate_lines(self):
        fields = parse_fields(TEXT_LAYER_P0003)
        assert fields["invoice_no"] == "140981024"
        assert fields["invoice_date"] == "2026/09/08"
        assert fields["bank_code"] == "024"
        assert fields["bank_account"] == "987654321"

    def test_caption_is_not_mistaken_for_a_value(self):
        """Regression: 'Payee Name  Bank Code  ...' must not yield a caption value."""
        fields = parse_fields(OCR_TEXT_P0001)
        assert "Bank Code" not in fields["payee_name"]
        assert fields["payee_name"] == "ABC Medical Centre Limited"

    def test_empty_text_yields_no_fields(self):
        assert parse_fields("") == {}


class TestGuessPayeeFromHeader:
    def test_uses_the_letterhead(self):
        assert (
            guess_payee_from_header(TEXT_LAYER_P0003)
            == "Healthy Imaging Centre Limited"
        )

    def test_skips_a_title_line(self):
        text = "TAX INVOICE\nHealthy Imaging Centre Limited\nTOTAL 1,000.00"
        assert guess_payee_from_header(text) == "Healthy Imaging Centre Limited"

    def test_skips_chinese_invoice_title(self):
        assert guess_payee_from_header(OCR_TEXT_P0002) == "XYZ Office Supplies Limited"


class TestParseInvoiceText:
    @pytest.mark.parametrize(
        ("text", "expected_total", "expected_payee"),
        [
            (OCR_TEXT_P0001, Decimal("15000.00"), "ABC Medical Centre Limited"),
            (OCR_TEXT_P0002, Decimal("5800.00"), "XYZ Office Supplies Limited"),
            (TEXT_LAYER_P0003, Decimal("120000.00"), "Healthy Imaging Centre Limited"),
        ],
    )
    def test_totals_and_payees(self, text, expected_total, expected_payee):
        document = parse_invoice_text(text, source="test.pdf")
        assert document.total_amount == expected_total
        assert document.payee_name == expected_payee

    def test_missing_amount_is_reported(self):
        document = parse_invoice_text("ABC Limited\nNo figures on this page")
        assert document.total_amount is None
        assert any("No amount figure" in w for w in document.warnings)

    def test_source_kind_is_recorded(self):
        document = parse_invoice_text(
            OCR_TEXT_P0001, source="scan.pdf", source_kind=SourceKind.OCR_LOCAL
        )
        assert document.source_kind is SourceKind.OCR_LOCAL
        assert document.filename == "scan.pdf"
