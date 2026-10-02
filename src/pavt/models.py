"""Domain models shared by the extraction, verification and export layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path


class MatchStatus(str, Enum):
    """Outcome of crosschecking one payment request against its invoice."""

    MATCHED = "MATCHED"
    """Invoice total equals the requested amount (within tolerance)."""

    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    """An invoice was matched to the request but the totals differ."""

    NO_INVOICE = "NO_INVOICE"
    """No supporting document could be matched to this request."""

    UNREADABLE = "UNREADABLE"
    """A document was matched but no amount could be extracted from it."""

    @property
    def passes(self) -> bool:
        """True when the request may enter the payment batch."""
        return self is MatchStatus.MATCHED


class SourceKind(str, Enum):
    """How the invoice text was obtained (shown in the audit trail)."""

    TEXT_LAYER = "TEXT_LAYER"
    OCR_LOCAL = "OCR_LOCAL"
    OCR_VISION = "OCR_VISION"
    NONE = "NONE"


@dataclass(slots=True)
class PaymentRequest:
    """One row of the Payment Request Summary."""

    request_no: str
    payee_name: str
    bank_code: str
    bank_account: str
    particulars: str
    amount: Decimal
    row_number: int = 0

    @property
    def bank_code_and_account(self) -> str:
        """Bank identifier as printed on the bank file, e.g. ``004-123456789``."""
        code = (self.bank_code or "").strip()
        account = (self.bank_account or "").strip()
        if code and account:
            return f"{code}-{account}"
        return account or code


@dataclass(slots=True)
class InvoiceDocument:
    """A supporting document after extraction and parsing."""

    source: Path
    text: str = ""
    source_kind: SourceKind = SourceKind.NONE
    page_count: int = 0
    total_amount: Decimal | None = None
    payee_name: str = ""
    bank_code: str = ""
    bank_account: str = ""
    invoice_no: str = ""
    invoice_date: str = ""
    amount_candidates: list[Decimal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return self.source.name


@dataclass(slots=True)
class VerificationResult:
    """The crosscheck verdict for a single payment request."""

    request: PaymentRequest
    status: MatchStatus
    invoice: InvoiceDocument | None = None
    invoice_amount: Decimal | None = None
    difference: Decimal | None = None
    match_confidence: float = 0.0
    match_reason: str = ""
    note: str = ""

    @property
    def included_in_bank_file(self) -> bool:
        """Only fully verified requests are paid out."""
        return self.status.passes
