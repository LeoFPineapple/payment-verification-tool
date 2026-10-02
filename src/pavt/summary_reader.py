"""Payment Request Summary (Excel) reader.

The summary is the file of record: payee name, bank code and account number in
the bank file all come from here, never from the invoice.  Parsing is therefore
strict -- we would rather refuse a malformed summary than guess at a bank account.

Column headers are matched by intent rather than by exact wording, so a summary
with slightly different captions (or Chinese ones) still loads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .models import PaymentRequest
from .money import to_decimal

REQUIRED_FIELDS = (
    "payee_name",
    "bank_code",
    "bank_account",
    "payment_amount",
)

# Logical field -> header cues.  Matched against a normalised (lower-case,
# punctuation-free) header cell, so "Payment Amount (HKD)" and "payment amount"
# are the same cue.
_HEADER_CUES: dict[str, tuple[str, ...]] = {
    "request_no": (
        "payment request no",
        "payment request number",
        "request no",
        "request number",
        "請求編號",
        "付款請求編號",
        "申請編號",
        "序號",
    ),
    "payee_name": (
        "payee name",
        "payee",
        "beneficiary name",
        "beneficiary",
        "收款人",
        "受款人",
        "供應商名稱",
        "供應商",
    ),
    "bank_code": ("bank code", "bankcode", "銀行編號", "银行编号", "銀行代號", "銀行代碼"),
    "bank_account": (
        "bank account number",
        "bank account no",
        "bank account",
        "account number",
        "account no",
        "銀行賬戶號碼",
        "銀行帳號",
        "银行账号",
        "戶口號碼",
        "帳號",
        "賬號",
    ),
    "particulars": ("particulars", "description", "details", "摘要", "項目", "用途"),
    "payment_amount": (
        "payment amount",
        "amount hkd",
        "amount",
        "payment amount hkd",
        "付款金額",
        "金額",
        "付款额",
    ),
}


def _normalise_header(value: object) -> str:
    if value is None:
        return ""
    text = str(value).casefold()
    text = re.sub(r"[（(].*?[)）]", " ", text)  # drop "(HKD)" style qualifiers
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _match_field(header: str) -> str | None:
    """Map one header cell to a logical field name."""
    if not header:
        return None
    # Longest cue first: "payment amount" must win over "amount".
    candidates = sorted(
        ((cue, field) for field, cues in _HEADER_CUES.items() for cue in cues),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for cue, field in candidates:
        if header == cue or cue in header:
            return field
    return None


@dataclass(slots=True)
class SummaryParseResult:
    """Parsed summary plus anything the operator needs to know about it."""

    requests: list[PaymentRequest] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_rows: list[str] = field(default_factory=list)
    header_map: dict[str, str] = field(default_factory=dict)
    sheet_name: str = ""


def _as_text(value: object) -> str:
    """Render a cell as text without Excel's float rendering of ids."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value).strip()


def _find_header_row(rows: list[tuple[int, tuple]]) -> tuple[int, dict[int, str]] | None:
    """Locate the header row and map column index -> logical field."""
    for index, (_row_number, values) in enumerate(rows[:20]):
        mapping: dict[int, str] = {}
        for column, value in enumerate(values):
            logical = _match_field(_normalise_header(value))
            if logical and logical not in mapping.values():
                mapping[column] = logical
        # A real header row identifies the fields we cannot proceed without.
        if all(any(f == value for value in mapping.values()) for f in ("payee_name", "payment_amount")):
            return index, mapping
    return None


def read_summary(path: str | Path, sheet_name: str | None = None) -> SummaryParseResult:
    """Read a Payment Request Summary workbook into :class:`PaymentRequest` rows.

    Args:
        path: ``.xlsx`` (or ``.xlsm``) summary file.
        sheet_name: Optional sheet to read; defaults to the first sheet.

    Returns:
        A :class:`SummaryParseResult`.  Rows missing a payee or an amount are
        recorded in ``skipped_rows`` instead of being silently dropped.
    """
    import openpyxl

    path = Path(path)
    result = SummaryParseResult()
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        worksheet = workbook[sheet_name] if sheet_name else workbook.worksheets[0]
        result.sheet_name = worksheet.title
        rows = [
            (number, tuple(cell.value for cell in row))
            for number, row in enumerate(worksheet.iter_rows(), start=1)
        ]
    finally:
        workbook.close()

    located = _find_header_row(rows)
    if not located:
        result.warnings.append(
            "Could not find a header row containing at least a Payee Name and a "
            "Payment Amount column. Please check the summary layout."
        )
        return result

    header_index, mapping = located
    result.header_map = {
        field: _as_text(rows[header_index][1][column]) for column, field in mapping.items()
    }
    missing = [f for f in REQUIRED_FIELDS if f not in mapping.values()]
    if missing:
        result.warnings.append(
            "Summary is missing expected column(s): "
            + ", ".join(sorted(missing))
            + ". Matching will be less reliable."
        )

    for row_number, values in rows[header_index + 1 :]:
        def cell(field_name: str) -> object:
            for column, logical in mapping.items():
                if logical == field_name and column < len(values):
                    return values[column]
            return None

        payee = _as_text(cell("payee_name"))
        amount = to_decimal(cell("payment_amount"))
        request_no = _as_text(cell("request_no"))

        if not payee and amount is None and not request_no:
            continue  # blank spacer row
        if not payee:
            result.skipped_rows.append(f"Row {row_number}: no payee name -- skipped.")
            continue
        if amount is None:
            result.skipped_rows.append(
                f"Row {row_number}: no readable payment amount for '{payee}' -- skipped."
            )
            continue

        result.requests.append(
            PaymentRequest(
                request_no=request_no or f"ROW{row_number}",
                payee_name=payee,
                bank_code=_as_text(cell("bank_code")),
                bank_account=_as_text(cell("bank_account")),
                particulars=_as_text(cell("particulars")),
                amount=amount,
                row_number=row_number,
            )
        )

    if not result.requests:
        result.warnings.append("No usable payment request rows were found in the summary.")
    return result
