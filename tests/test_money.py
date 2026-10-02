"""Unit tests for amount parsing and formatting.

These guard the most safety-critical logic in the tool: deciding whether a figure
on an invoice is money, and which figure is the payable total.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pavt.money import (
    amounts_equal,
    extract_total_amount,
    find_amounts,
    format_amount,
    format_amount_display,
    normalise_digits,
    normalise_name,
    normalise_ocr_numbers,
    quantize,
    to_decimal,
)


class TestToDecimal:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (15000, Decimal("15000")),
            (5800.5, Decimal("5800.5")),
            ("120,000.00", Decimal("120000.00")),
            ("HKD 1,230", Decimal("1230")),
            ("$15,000.00", Decimal("15000.00")),
            ("", None),
            (None, None),
            ("not a number", None),
            (True, None),  # a bool is not money
        ],
    )
    def test_converts(self, value, expected):
        assert to_decimal(value) == expected

    def test_float_does_not_introduce_binary_noise(self):
        # Decimal(0.1) would be 0.1000000000000000055511151231257827...
        assert to_decimal(0.1) == Decimal("0.1")


class TestFindAmounts:
    def test_reads_grouped_and_decimal_amounts(self):
        amounts = [a for a, _, _ in find_amounts("Imaging Service Fee  120,000.00")]
        assert amounts == [Decimal("120000.00")]

    def test_currency_prefix_is_recognised(self):
        amounts = [a for a, _, _ in find_amounts("Total Amount: HKD 15,000.00")]
        assert Decimal("15000.00") in amounts

    def test_invoice_number_is_not_money(self):
        assert find_amounts("Invoice No. 140981024") == []

    def test_bank_account_is_not_money(self):
        assert find_amounts("Bank Account Number 987654321") == []

    def test_date_is_not_money(self):
        assert find_amounts("Invoice Date 2026/09/08") == []

    def test_bare_integer_is_not_money(self):
        # Too ambiguous: could be a quantity, a page count, a box number.
        assert find_amounts("Quantity 12") == []

    def test_excessive_precision_is_rejected(self):
        assert find_amounts("rate 1.23456") == []


class TestExtractTotalAmount:
    def test_english_total_line(self):
        text = "Particulars  Amount (HKD)\nImaging Service Fee  120,000.00\nTOTAL  120,000.00"
        assert extract_total_amount(text) == Decimal("120000.00")

    def test_total_amount_phrase(self):
        text = "Clinic Service Fee  15,000.00\nTotal Amount: HKD 15,000.00"
        assert extract_total_amount(text) == Decimal("15000.00")

    def test_traditional_chinese_total(self):
        text = "辨公室文具  5,800.00\n總計  5,800.00"
        assert extract_total_amount(text) == Decimal("5800.00")

    def test_last_total_wins_when_repeated(self):
        text = "TOTAL  1,000.00\nTOTAL  2,000.00"
        assert extract_total_amount(text) == Decimal("2000.00")

    def test_subtotal_is_not_treated_as_the_total(self):
        text = "Sub-total  1,000.00\nTOTAL  1,200.00"
        assert extract_total_amount(text) == Decimal("1200.00")

    def test_largest_amount_is_used_without_a_total_cue(self):
        text = "Line one  100.00\nLine two  9,999.00\nLine three  250.00"
        assert extract_total_amount(text) == Decimal("9999.00")

    def test_returns_none_for_empty_text(self):
        assert extract_total_amount("") is None
        assert extract_total_amount("no figures here") is None

    def test_mismatched_invoice_regression(self):
        """The supplied P0003 scan: 120,000.00 must not read as 125,000.00."""
        text = (
            "Healthy Imaging Centre Limited\n"
            "TAX INVOICE\nInvoice No.\n140981024\n"
            "Invoice Date\n2026/09/08\nDue Date\n2026/11/08\n"
            "Bank Code\n024\nBank Account Number\n987654321\n"
            "Particulars\nAmount (HKD)\nImaging Service Fee\n120,000.00\n"
            "TOTAL\n120,000.00"
        )
        assert extract_total_amount(text) == Decimal("120000.00")


class TestOcrNumberRepair:
    """Regressions for misreads actually produced by the OCR models.

    The older PP-OCRv3 model (RapidOCR 1.2.x, which is what a Python 3.13 pip
    install resolves to) reads "15,000.00" as "15,ooo.o0" and "5,800.00" as
    "5,8oo.oo".  Reading those literally would report 15.00 and no amount at all --
    a wrong payment rather than a held one.
    """

    @pytest.mark.parametrize(
        ("ocr_text", "expected"),
        [
            ("15,ooo.o0", "15,000.00"),
            ("5,8oo.oo", "5,800.00"),
            ("15,ooo.oo", "15,000.00"),
            ("12,OOO.00", "12,000.00"),
            ("15.000.00", "15,000.00"),  # period used as a thousands separator
            ("HKD 15,000.00", "HKD 15,000.00"),  # already correct: untouched
        ],
    )
    def test_repairs_misread_digits(self, ocr_text, expected):
        assert normalise_ocr_numbers(ocr_text) == expected

    @pytest.mark.parametrize("text", ["ABCO123", "ABCO 123", "ABCO.x", "Total OOO"])
    def test_leaves_letters_in_identifiers_alone(self, text):
        """A letter-O in a code must not silently become a digit."""
        assert normalise_ocr_numbers(text) == text

    def test_misread_amounts_are_read_correctly(self):
        assert [a for a, _, _ in find_amounts("15,ooo.o0")] == [Decimal("15000.00")]
        assert [a for a, _, _ in find_amounts("5,8oo.oo")] == [Decimal("5800.00")]

    def test_misread_total_lines_are_read_correctly(self):
        assert extract_total_amount("Total Amount:HkD 15,ooo.o0") == Decimal("15000.00")
        assert extract_total_amount("總計  5,8oo.oo") == Decimal("5800.00")
        assert extract_total_amount("Total 15,ooo.oo") == Decimal("15000.00")

    def test_repair_does_not_weaken_the_rejection_rules(self):
        """The repair must not turn non-amounts into amounts."""
        assert find_amounts("rate 1.23456") == []
        assert find_amounts("Invoice No. 140981024") == []
        assert find_amounts("Bank Account Number 987654321") == []
        assert find_amounts("Invoice Date 2026/09/08") == []


class TestComparisonAndFormatting:
    def test_quantize_rounds_half_up(self):
        assert quantize(Decimal("1.005")) == Decimal("1.01")
        assert quantize(Decimal("1.004")) == Decimal("1.00")

    def test_amounts_equal_within_tolerance(self):
        assert amounts_equal(Decimal("100.00"), Decimal("100.01"), Decimal("0.01"))
        assert not amounts_equal(Decimal("100.00"), Decimal("100.02"), Decimal("0.01"))

    def test_zero_tolerance_is_exact(self):
        assert amounts_equal(Decimal("100.00"), Decimal("100.00"), Decimal("0"))
        assert not amounts_equal(Decimal("100.00"), Decimal("100.01"), Decimal("0"))

    def test_formatting(self):
        assert format_amount(Decimal("120000")) == "120000.00"
        assert format_amount_display(Decimal("120000")) == "120,000.00"
        assert format_amount(None) == ""


class TestNormalisation:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("ABC Medical Centre Limited", "ABCMedical CentreLimited"),
            ("ABC Medical Centre Ltd.", "abc medical centre limited"),
            ("XYZ Office Supplies Limited", "XYZ Office Supplies Ltd"),
        ],
    )
    def test_equivalent_names_fold_together(self, left, right):
        assert normalise_name(left) == normalise_name(right)

    def test_different_names_stay_different(self):
        assert normalise_name("ABC Medical Centre Limited") != normalise_name(
            "Healthy Imaging Centre Limited"
        )

    def test_digits_normalisation(self):
        assert normalise_digits("004-123456789") == "004123456789"
        assert normalise_digits(None) == ""
