"""Crosscheck engine: pair each payment request with its supporting document.

Design principle
----------------
**A false mismatch is an inconvenience; a false match is a wrong payment.**
When the evidence is ambiguous the engine refuses to pair, because an unpaired
request is excluded from the bank file and therefore fails safe.  Every decision
records its reason and confidence so an operator can audit *why* a payment was
held back.

Pairing runs in four tiers:

1. payee name similarity, boosted by a matching bank code/account and by an
   agreeing amount;
2. bank code + account number, when the payee name was garbled by OCR;
3. amount, when exactly one unmatched invoice carries that exact total;
4. give up -- the request is reported as ``NO_INVOICE``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher

from .models import InvoiceDocument, MatchStatus, PaymentRequest, VerificationResult
from .money import amounts_equal, normalise_digits, normalise_name, quantize

# Below this score a name match is treated as coincidence, not evidence.
NAME_MATCH_THRESHOLD = 0.72
# A weaker score is still accepted when the bank account also agrees.
NAME_MATCH_THRESHOLD_WITH_ACCOUNT = 0.55


def name_similarity(left: str, right: str) -> float:
    """Similarity of two payee names in ``[0, 1]``, robust to OCR spacing noise."""
    a, b = normalise_name(left), normalise_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        # "ABCMedical" vs "ABCMedicalCentre" -- related, but not an identity.
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


@dataclass(slots=True)
class Pairing:
    """The chosen invoice for one request, with the evidence behind the choice."""

    request: PaymentRequest
    invoice: InvoiceDocument | None
    confidence: float
    reason: str


@dataclass(slots=True)
class MatchOutcome:
    """Result of pairing a whole batch."""

    pairings: list[Pairing] = field(default_factory=list)
    unmatched_invoices: list[InvoiceDocument] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_usable(invoice: InvoiceDocument) -> bool:
    """True when an invoice carries a total and can therefore support a payment.

    A document whose amount could not be read must never be *matched* to a
    request: doing so would invent support that does not exist.  Such documents
    are still reported separately, and a request that appears to belong to one is
    flagged as ``UNREADABLE`` rather than silently passing or failing.
    """
    return invoice.total_amount is not None


def _candidate_score(
    request: PaymentRequest,
    invoice: InvoiceDocument,
    tolerance: Decimal,
) -> tuple[float, str]:
    """Score how well ``invoice`` supports ``request``.

    The amount agreeing is strong evidence but is deliberately *not* sufficient
    on its own here -- two different suppliers can bill the same round figure --
    so it acts as a boost on top of an identity signal.
    """
    similarity = name_similarity(request.payee_name, invoice.payee_name)

    account_agrees = bool(request.bank_account and invoice.bank_account) and (
        normalise_digits(request.bank_account) == normalise_digits(invoice.bank_account)
    )
    code_agrees = bool(request.bank_code and invoice.bank_code) and (
        normalise_digits(request.bank_code) == normalise_digits(invoice.bank_code)
    )
    account_evidence = account_agrees and (code_agrees or not invoice.bank_code)

    amount_agrees = (
        invoice.total_amount is not None
        and amounts_equal(request.amount, invoice.total_amount, tolerance)
    )

    score = similarity
    notes: list[str] = []
    if similarity:
        notes.append(f"name similarity {similarity:.2f}")
    if account_evidence:
        score = max(score, NAME_MATCH_THRESHOLD_WITH_ACCOUNT) + 0.1
        notes.append("bank account matches")
    if amount_agrees:
        score += 0.05
        notes.append("invoice total agrees with the requested amount")

    # Genuine identity evidence must exist; amount agreement alone is not enough
    # to pair, otherwise every equal-value invoice would become a candidate.
    name_is_evidence = similarity >= NAME_MATCH_THRESHOLD
    if name_is_evidence or account_evidence:
        return min(score, 1.0), ", ".join(notes) or "identity match"
    return 0.0, "no shared payee name or bank account"


def _name_is_readable(name: str) -> bool:
    """True when a payee name carries enough signal to be compared at all.

    Gates the amount-only rescue: if we could read the payee name and it does not
    match, that is *evidence against* the pair, and a coincidentally equal amount
    must not override it.  The rescue exists only for names OCR destroyed.
    """
    letters = normalise_name(name)
    return len(letters) >= 4


def pair_requests_with_invoices(
    requests: list[PaymentRequest],
    invoices: list[InvoiceDocument],
    *,
    tolerance: Decimal = Decimal("0.01"),
) -> MatchOutcome:
    """Assign at most one invoice to each request, highest confidence first."""
    outcome = MatchOutcome()
    # Documents with no readable amount cannot support any request.
    usable = [invoice for invoice in invoices if is_usable(invoice)]
    unusable = [invoice for invoice in invoices if not is_usable(invoice)]

    # Build every plausible (request, invoice) pair, then take them greedily from
    # the strongest down.  Greedy with a confidence ordering is stable and easy to
    # explain to an auditor, which matters more here than optimality.
    scored: list[tuple[float, int, int, str]] = []
    for r_index, request in enumerate(requests):
        for i_index, invoice in enumerate(usable):
            score, reason = _candidate_score(request, invoice, tolerance)
            if score > 0:
                scored.append((score, r_index, i_index, reason))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))

    assigned_requests: set[int] = set()
    assigned_invoices: set[int] = set()
    chosen: dict[int, Pairing] = {}

    for score, r_index, i_index, reason in scored:
        if r_index in assigned_requests or i_index in assigned_invoices:
            continue
        assigned_requests.add(r_index)
        assigned_invoices.add(i_index)
        chosen[r_index] = Pairing(requests[r_index], usable[i_index], score, reason)

    # Tier 3: rescue requests whose payee name OCR destroyed, but only where a
    # unique unmatched invoice carries exactly the requested amount.  A readable
    # name that simply did not match is treated as evidence *against* the pair, so
    # the rescue is skipped -- otherwise any two invoices sharing a round figure
    # would be cross-paired and, worse, reported as an amount mismatch rather than
    # as a missing document.
    for r_index, request in enumerate(requests):
        if r_index in assigned_requests or _name_is_readable(request.payee_name):
            continue
        exact = [
            (i_index, invoice)
            for i_index, invoice in enumerate(usable)
            if i_index not in assigned_invoices
            and invoice.total_amount is not None
            and amounts_equal(request.amount, invoice.total_amount, tolerance)
        ]
        if len(exact) == 1:
            i_index, invoice = exact[0]
            assigned_requests.add(r_index)
            assigned_invoices.add(i_index)
            chosen[r_index] = Pairing(
                request,
                invoice,
                0.5,
                "payee name unreadable; paired by unique matching invoice amount",
            )

    # Anyone still unpaired: prefer to report the unreadable document that looks
    # like theirs, so the operator sees "amount unreadable" rather than the much
    # less useful "no invoice supplied".
    for r_index, request in enumerate(requests):
        if r_index in chosen:
            continue
        likely = [
            invoice
            for invoice in unusable
            if name_similarity(request.payee_name, invoice.payee_name)
            >= NAME_MATCH_THRESHOLD
        ]
        if likely:
            chosen[r_index] = Pairing(
                request,
                likely[0],
                0.0,
                "document matched by payee name but its amount could not be read",
            )
        else:
            chosen[r_index] = Pairing(
                request, None, 0.0, "no supporting document matched"
            )

    outcome.pairings = [chosen[r_index] for r_index in range(len(requests))]
    outcome.unmatched_invoices = [
        invoice for index, invoice in enumerate(usable) if index not in assigned_invoices
    ] + unusable
    return outcome


def verify_payment(
    request: PaymentRequest,
    invoice: InvoiceDocument | None,
    *,
    tolerance: Decimal = Decimal("0.01"),
    match_confidence: float = 0.0,
    match_reason: str = "",
) -> VerificationResult:
    """Compare one request against its paired invoice and return a verdict."""
    if invoice is None:
        return VerificationResult(
            request=request,
            status=MatchStatus.NO_INVOICE,
            match_confidence=match_confidence,
            match_reason=match_reason or "no supporting document matched",
            note="No invoice could be matched to this payment request; excluded from the bank file.",
        )

    if invoice.total_amount is None:
        return VerificationResult(
            request=request,
            invoice=invoice,
            status=MatchStatus.UNREADABLE,
            match_confidence=match_confidence,
            match_reason=match_reason,
            note=(
                f"Invoice '{invoice.filename}' was read via "
                f"{invoice.source_kind.value} but no amount could be extracted; "
                "excluded from the bank file."
            ),
        )

    difference = quantize(request.amount) - quantize(invoice.total_amount)
    if amounts_equal(request.amount, invoice.total_amount, tolerance):
        return VerificationResult(
            request=request,
            invoice=invoice,
            status=MatchStatus.MATCHED,
            invoice_amount=invoice.total_amount,
            difference=difference,
            match_confidence=match_confidence,
            match_reason=match_reason,
            note=f"Verified against '{invoice.filename}'.",
        )

    return VerificationResult(
        request=request,
        invoice=invoice,
        status=MatchStatus.AMOUNT_MISMATCH,
        invoice_amount=invoice.total_amount,
        difference=difference,
        match_confidence=match_confidence,
        match_reason=match_reason,
        note=(
            f"Requested {request.amount:.2f} but invoice '{invoice.filename}' shows "
            f"{invoice.total_amount:.2f} (difference {difference:+.2f}); excluded from the bank file."
        ),
    )


def verify_batch(
    requests: list[PaymentRequest],
    invoices: list[InvoiceDocument],
    *,
    tolerance: Decimal = Decimal("0.01"),
) -> tuple[list[VerificationResult], MatchOutcome]:
    """Pair a whole batch, then verify every request against its invoice."""
    outcome = pair_requests_with_invoices(requests, invoices, tolerance=tolerance)
    results = [
        verify_payment(
            pairing.request,
            pairing.invoice,
            tolerance=tolerance,
            match_confidence=pairing.confidence,
            match_reason=pairing.reason,
        )
        for pairing in outcome.pairings
    ]
    return results, outcome
