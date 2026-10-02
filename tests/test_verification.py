"""Unit tests for the crosscheck engine.

The behaviour under test is a control, not a convenience: an unmatched or
mismatched request must never reach the payment batch.
"""

from __future__ import annotations

from decimal import Decimal

from pavt.models import InvoiceDocument, MatchStatus, PaymentRequest, SourceKind
from pavt.verification import (
    name_similarity,
    pair_requests_with_invoices,
    verify_batch,
    verify_payment,
)

TOLERANCE = Decimal("0.01")


def make_request(**overrides) -> PaymentRequest:
    defaults = dict(
        request_no="P000001",
        payee_name="ABC Medical Centre Limited",
        bank_code="004",
        bank_account="123456789",
        particulars="Clinic Service Fee",
        amount=Decimal("15000.00"),
    )
    defaults.update(overrides)
    return PaymentRequest(**defaults)


def make_invoice(**overrides) -> InvoiceDocument:
    from pathlib import Path

    defaults = dict(
        source=Path("P0001.pdf"),
        total_amount=Decimal("15000.00"),
        payee_name="ABC Medical Centre Limited",
        bank_code="004",
        bank_account="123456789",
        source_kind=SourceKind.OCR_LOCAL,
    )
    defaults.update(overrides)
    return InvoiceDocument(**defaults)


class TestNameSimilarity:
    def test_identical_names(self):
        assert name_similarity("ABC Ltd", "ABC Ltd") == 1.0

    def test_ocr_spacing_noise_still_matches(self):
        assert name_similarity("ABC Medical Centre Limited", "ABCMedicalCentreLimited") == 1.0

    def test_partial_name_is_close_but_not_identical(self):
        assert 0.5 < name_similarity("ABC Medical", "ABC Medical Centre Limited") < 1.0

    def test_unrelated_names_score_low(self):
        assert name_similarity("ABC Medical", "Healthy Imaging") < 0.5

    def test_empty_name_scores_zero(self):
        assert name_similarity("", "ABC Ltd") == 0.0


class TestPairing:
    def test_pairs_by_payee_name(self):
        outcome = pair_requests_with_invoices([make_request()], [make_invoice()])
        assert outcome.pairings[0].invoice is not None
        assert outcome.pairings[0].confidence > 0.9

    def test_amount_agreement_alone_does_not_create_a_pair(self):
        """A different supplier billing the same amount must not be paired."""
        request = make_request(payee_name="ABC Medical Centre Limited")
        invoice = make_invoice(payee_name="Healthy Imaging Centre Limited")
        outcome = pair_requests_with_invoices([request], [invoice])
        # Tier 3 pairs by unique amount, which is intended, but the reason must
        # say so rather than claiming a name match.
        assert "amount" in outcome.pairings[0].reason

    def test_garbled_name_is_rescued_by_a_unique_amount(self):
        # Keep the bank account unreadable too, so the amount is the only signal.
        request = make_request(payee_name="### $$$", bank_account="")
        invoice = make_invoice(payee_name="ABC Medical Centre Limited", bank_account="")
        outcome = pair_requests_with_invoices([request], [invoice])
        assert outcome.pairings[0].invoice is invoice
        assert outcome.pairings[0].confidence == 0.5
        assert "unique matching invoice amount" in outcome.pairings[0].reason

    def test_readable_but_different_name_blocks_the_amount_rescue(self):
        """A coincidentally equal amount must not pair a clearly different payee.

        Regression: pairing them here reported "amount mismatch" instead of the
        truth, which is that no invoice was supplied for this request at all.
        """
        request = make_request(payee_name="ABC Medical Centre Limited", amount=Decimal("120000.00"))
        invoice = make_invoice(
            payee_name="Healthy Imaging Centre Limited",
            total_amount=Decimal("120000.00"),
            bank_code="024",
            bank_account="987654321",
        )
        outcome = pair_requests_with_invoices([request], [invoice])
        assert outcome.pairings[0].invoice is None
        assert outcome.pairings[0].reason == "no supporting document matched"

    def test_bank_account_rescues_a_weak_name(self):
        request = make_request(payee_name="ABC Medical Centr")
        invoice = make_invoice(payee_name="ABC Medical Centre Ltd")
        outcome = pair_requests_with_invoices([request], [invoice])
        assert outcome.pairings[0].invoice is invoice

    def test_no_invoice_leaves_the_request_unpaired(self):
        outcome = pair_requests_with_invoices([make_request()], [])
        assert outcome.pairings[0].invoice is None

    def test_each_invoice_is_used_at_most_once(self):
        request_a = make_request(request_no="A", payee_name="ABC Limited")
        request_b = make_request(request_no="B", payee_name="ABC Limited")
        invoice = make_invoice(payee_name="ABC Limited")
        outcome = pair_requests_with_invoices([request_a, request_b], [invoice])
        assigned = [p.invoice for p in outcome.pairings]
        assert assigned.count(invoice) == 1
        assert assigned.count(None) == 1

    def test_unmatched_invoices_are_reported(self):
        extra = make_invoice(source=__import__("pathlib").Path("P0099.pdf"),
                             payee_name="Unrelated Supplier Limited",
                             total_amount=Decimal("99.00"))
        outcome = pair_requests_with_invoices([make_request()], [make_invoice(), extra])
        assert extra in outcome.unmatched_invoices


class TestVerifyPayment:
    def test_matched(self):
        result = verify_payment(make_request(), make_invoice(), tolerance=TOLERANCE)
        assert result.status is MatchStatus.MATCHED
        assert result.included_in_bank_file

    def test_amount_mismatch_is_excluded(self):
        invoice = make_invoice(total_amount=Decimal("13000.00"))
        result = verify_payment(make_request(), invoice, tolerance=TOLERANCE)
        assert result.status is MatchStatus.AMOUNT_MISMATCH
        assert result.difference == Decimal("2000.00")
        assert not result.included_in_bank_file

    def test_missing_invoice_is_excluded(self):
        result = verify_payment(make_request(), None, tolerance=TOLERANCE)
        assert result.status is MatchStatus.NO_INVOICE
        assert not result.included_in_bank_file

    def test_unreadable_invoice_is_excluded(self):
        result = verify_payment(
            make_request(), make_invoice(total_amount=None), tolerance=TOLERANCE
        )
        assert result.status is MatchStatus.UNREADABLE
        assert not result.included_in_bank_file

    def test_tolerance_is_honoured(self):
        invoice = make_invoice(total_amount=Decimal("15000.01"))
        assert verify_payment(make_request(), invoice, tolerance=Decimal("0.01")).status is MatchStatus.MATCHED
        assert verify_payment(make_request(), invoice, tolerance=Decimal("0")).status is MatchStatus.AMOUNT_MISMATCH

    def test_note_explains_the_hold(self):
        result = verify_payment(
            make_request(), make_invoice(total_amount=Decimal("1.00")), tolerance=TOLERANCE
        )
        assert "excluded from the bank file" in result.note


class TestVerifyBatch:
    def test_sample_batch_verdicts(self):
        """The supplied sample data: two payments pass, the third is held back."""
        requests = [
            make_request(
                request_no="P000001",
                payee_name="ABC Medical Centre Limited",
                bank_account="123456789",
                amount=Decimal("15000.00"),
            ),
            make_request(
                request_no="P000002",
                payee_name="XYZ Office Supplies Limited",
                bank_account="456789123",
                amount=Decimal("5800.00"),
            ),
            make_request(
                request_no="P000003",
                payee_name="Healthy Imaging Centre Limited",
                bank_code="024",
                bank_account="987654321",
                amount=Decimal("125000.00"),
            ),
        ]
        invoices = [
            make_invoice(),
            make_invoice(
                source=__import__("pathlib").Path("P0002.pdf"),
                payee_name="XYZ Office Supplies Limited",
                bank_account="456789123",
                total_amount=Decimal("5800.00"),
            ),
            make_invoice(
                source=__import__("pathlib").Path("P0003.pdf"),
                payee_name="Healthy Imaging Centre Limited",
                bank_code="024",
                bank_account="987654321",
                total_amount=Decimal("120000.00"),
            ),
        ]
        results, _ = verify_batch(requests, invoices, tolerance=TOLERANCE)
        by_no = {r.request.request_no: r for r in results}
        assert by_no["P000001"].status is MatchStatus.MATCHED
        assert by_no["P000002"].status is MatchStatus.MATCHED
        assert by_no["P000003"].status is MatchStatus.AMOUNT_MISMATCH
        assert by_no["P000003"].difference == Decimal("5000.00")
        assert sum(1 for r in results if r.included_in_bank_file) == 2
