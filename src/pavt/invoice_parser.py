"""Template-independent invoice field parsing.

Invoices arrive in as many layouts as there are suppliers -- English, traditional
Chinese, ``TOTAL`` versus ``總計``, labels that OCR has glued together
(``BankAccountNumber``), and table layouts where the captions sit on one visual
row and the figures on the next.

Rather than matching one fixed template, this module works by *label intent*: each
logical field declares a set of cues, and the value is taken from the same table
cell as the cue when possible, from the row below when the caption row is a table
header, or from the following line when the label simply ends the line.

Only ``total_amount`` feeds the verification decision.  The other fields are
extracted to build the audit trail and to score how confidently an invoice belongs
to a given payment request.
"""

from __future__ import annotations

import re
from pathlib import Path

from .extraction import ExtractionResult, extract_document
from .models import InvoiceDocument, SourceKind
from .money import extract_total_amount, find_amounts, normalise_digits

# Field cue table.  Keys are logical field names; values are regex alternatives.
_FIELD_CUES: dict[str, str] = {
    # Note: bare "from" is deliberately absent -- it matches the "m" in
    # "XYZ Office Supplies Limited" and would hijack the letterhead line.
    "payee_name": r"payee\s*name|收款人|受款人|供應商名稱|供应商名称|供應商|供应商|vendor\s*name",
    "bank_code": r"bank\s*code|[銀银]\s*行\s*[編编]\s*[號号]|[銀银]行代[號号碼码]",
    "bank_account": (
        # 賬/帳/帐/眼 and 戶/户 both occur, depending on the OCR result.
        r"bank\s*account\s*(?:number|no\.?|號碼|号码)?"
        r"|[銀银]\s*行\s*[賬帳帐眼]\s*[戶户]\s*[號号]?\s*[碼码]?"
        r"|[戶户]\s*口\s*[號号]\s*[碼码]"
        r"|[账帳賬]\s*[號号]"
    ),
    "invoice_no": (
        r"invoice\s*(?:no\.?|number|#)|發票[編编][號号]|发票编号|[單单]\s*[號号]"
    ),
    "invoice_date": r"invoice\s*date|發票日期|发票日期|開立日期|日期",
    "due_date": r"due\s*date|到期日|付款期限",
}

# Characters OCR commonly returns in full-width form, plus the traditional /
# simplified pairs that share a shape but not a code point.
_CHAR_FOLD = str.maketrans(
    {
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
        "．": ".", "，": ",", "：": ":", "（": "(", "）": ")",
        "號": "号", "碼": "码", "賬": "账", "帳": "帐",
        "銀": "银", "編": "编", "總": "总", "計": "计",
    }
)

# Value-shape guards: a bank account is digits, a date is a date, ...
_DIGITS_ONLY = re.compile(r"^[\d\-\s]+$")
_DATE_LIKE = re.compile(r"\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}")
_INVOICE_NO_LIKE = re.compile(r"^[A-Za-z]{0,4}[\d\-/]{4,20}$")

# Column separator produced by ``pavt.extraction.group_boxes_into_lines``.
_CELL_SPLIT = re.compile(r"\s{2,}")

# Words that must never be mistaken for a payee name.
_NON_NAME = re.compile(
    r"^(?:tax\s+)?invoice\b|^發票|^发票|^receipt\b|^收據$|^bill$",
    re.IGNORECASE,
)


def fold_text(text: str) -> str:
    """Normalise full-width and traditional/simplified variants for matching."""
    return (text or "").translate(_CHAR_FOLD)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _cells(line: str) -> list[str]:
    """Split one visual row into table cells."""
    return [cell.strip() for cell in _CELL_SPLIT.split(line.strip()) if cell.strip()]


def _value_matches_field(field: str, value: str) -> bool:
    """Shape-check a candidate value for a logical field."""
    value = value.strip()
    if not value:
        return False
    if field == "bank_account":
        return bool(_DIGITS_ONLY.match(value))
    if field == "bank_code":
        return bool(re.fullmatch(r"\d{3}", value))
    if field in ("invoice_date", "due_date"):
        return bool(_DATE_LIKE.search(value))
    if field == "invoice_no":
        return bool(_INVOICE_NO_LIKE.match(value))
    return True


def _looks_like_a_label(value: str) -> bool:
    """True when ``value`` is another field's caption rather than a value.

    Needed because invoice tables put all their captions on one visual row
    (``Payee Name  Bank Code  Bank Account Number``); without this guard the
    caption to the right of a cue is read as that field's value.

    An empty string is *not* a label: it simply means the cue ended its cell, and
    the caller should keep looking at other candidates.
    """
    if not value:
        return False
    return any(
        re.search(other_cue, value, re.IGNORECASE) for other_cue in _FIELD_CUES.values()
    )


def _cue_fields(cell: str) -> list[str]:
    """Logical fields whose cue appears in ``cell``."""
    found: list[str] = []
    for field, cue in _FIELD_CUES.items():
        if re.search(cue, cell, re.IGNORECASE):
            found.append(field)
    return found


def _cell_after_cue(cell: str, cue: str) -> str:
    """Text following ``cue`` inside the same cell, minus separators."""
    match = re.search(cue, cell, re.IGNORECASE)
    if not match:
        return ""
    remainder = cell[match.end() :]
    return re.sub(r"^[\s:：\-–—=]+", "", remainder).strip()


def _select_value(
    field: str,
    candidates: list[tuple[int, str]],
) -> str | None:
    """Choose the best candidate for ``field``.

    ``candidates`` holds ``(priority, value)`` pairs where priority 0 means the
    value sat in the same cell as the cue and 1 means it came from elsewhere.
    A shape-valid value always beats an invalid one; beyond that, proximity to
    the cue decides.
    """
    best: tuple[bool, int, str] | None = None
    for priority, value in candidates:
        if not value or _looks_like_a_label(value):
            continue
        valid = _value_matches_field(field, value)
        key = (valid, -priority)
        if best is None or key > (best[0], best[1]):
            best = (valid, -priority, value)
    return best[2] if best else None


# --------------------------------------------------------------------------- #
# Field extraction
# --------------------------------------------------------------------------- #


def _extract_from_table_header(
    lines: list[str], index: int, fields: dict[str, str]
) -> None:
    """Map a caption row onto the data row underneath it.

    Handles the very common layout where captions and values are separate visual
    rows, e.g.::

        Payee Name  Bank Code  Bank Account Number
        ABC Medical Centre Limited  004  123456789

    Caption cells that continue a previous caption (``Bank Account Number`` split
    over two cells) are merged before columns are matched, so the caption count
    lines up with the value count.
    """
    header_cells = _cells(lines[index])
    if len(header_cells) < 2 or index + 1 >= len(lines):
        return
    value_cells = _cells(lines[index + 1])
    if not value_cells:
        return

    columns: list[list[str]] = []
    for cell in header_cells:
        matched = _cue_fields(cell)
        if not matched and columns and not any(ch.isdigit() for ch in cell):
            # A caption split across cells by the OCR layout, e.g.
            # "Bank Account" then "Number": re-read the two cells as one caption.
            merged = _cue_fields(header_cells[header_cells.index(cell) - 1] + " " + cell)
            if merged:
                columns[-1] = merged
                continue
        if matched:
            columns.append(matched)
    if len(columns) < 2 or len(columns) > len(value_cells):
        return  # ambiguous: fall back to per-cell extraction

    for position, column_fields in enumerate(columns):
        value = value_cells[position]
        for field in column_fields:
            if field in fields or not _value_matches_field(field, value):
                continue
            fields[field] = value


def parse_fields(text: str) -> dict[str, str]:
    """Extract labelled fields from invoice text."""
    normalised = fold_text(text)
    lines = _lines(normalised)
    fields: dict[str, str] = {}

    for index, line in enumerate(lines):
        cells = _cells(line)
        for position, cell in enumerate(cells):
            for field, cue in _FIELD_CUES.items():
                if field in fields:
                    continue
                if not re.search(cue, cell, re.IGNORECASE):
                    continue
                # Candidate order, most local first:
                #   0 - the rest of the cue's own cell ("Bank Code 004")
                #   1 - the next cell on the same visual row (caption | value)
                #   2 - the first cell of the next line (caption, then value)
                candidates = [(0, _cell_after_cue(cell, cue))]
                if position + 1 < len(cells):
                    candidates.append((1, cells[position + 1]))
                if len(cells) == 1 and index + 1 < len(lines):
                    following = _cells(lines[index + 1])
                    if following:
                        candidates.append((2, following[0]))
                value = _select_value(field, candidates)
                if value:
                    fields[field] = value
        if len(fields) == len(_FIELD_CUES):
            break

    # Table layouts where the captions sit on a row of their own.
    for index, line in enumerate(lines):
        if sum(1 for cell in _cells(line) if _cue_fields(cell)) >= 2:
            _extract_from_table_header(lines, index, fields)

    return {key: value for key, value in fields.items() if value}


# --------------------------------------------------------------------------- #
# Payee fallback and document assembly
# --------------------------------------------------------------------------- #


def guess_payee_from_header(text: str, max_lines: int = 6) -> str:
    """Fall back to the letterhead when no ``Payee Name`` label exists.

    Supplier invoices normally print the supplier's own name at the very top.
    """
    for line in _lines(fold_text(text))[:max_lines]:
        candidate = _cells(line)[0] if _cells(line) else ""
        if len(candidate) < 4 or _NON_NAME.search(candidate):
            continue
        if _DATE_LIKE.search(candidate) or any(ch.isdigit() for ch in candidate):
            continue
        if _looks_like_a_label(candidate):
            continue
        # A letterhead is mostly letters (incl. CJK); skip label-ish rows.
        letters = sum(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in candidate)
        if letters / len(candidate) > 0.6:
            return candidate
    return ""


def parse_invoice_text(
    text: str,
    source: str | Path = "<memory>",
    *,
    source_kind: SourceKind = SourceKind.NONE,
    page_count: int = 0,
    warnings: list[str] | None = None,
) -> InvoiceDocument:
    """Turn raw invoice text into a structured :class:`InvoiceDocument`."""
    document = InvoiceDocument(
        source=Path(source),
        text=text or "",
        source_kind=source_kind,
        page_count=page_count,
        warnings=list(warnings or []),
    )

    fields = parse_fields(document.text)
    document.payee_name = fields.get("payee_name", "") or guess_payee_from_header(
        document.text
    )
    document.bank_code = fields.get("bank_code", "")
    document.bank_account = normalise_digits(fields.get("bank_account", ""))
    document.invoice_no = fields.get("invoice_no", "")
    document.invoice_date = fields.get("invoice_date", "")

    document.amount_candidates = [amount for amount, _, _ in find_amounts(document.text)]

    total = extract_total_amount(fold_text(document.text))
    if total is None:
        document.warnings.append("No amount figure could be located on the document.")
    document.total_amount = total

    return document


def parse_invoice(path: str | Path, **extract_kwargs) -> InvoiceDocument:
    """Extract and parse a single invoice PDF."""
    result: ExtractionResult = extract_document(path, **extract_kwargs)
    return parse_invoice_text(
        result.text,
        source=path,
        source_kind=result.source_kind,
        page_count=result.page_count,
        warnings=list(result.warnings),
    )


def parse_invoices(paths, **extract_kwargs) -> list[InvoiceDocument]:
    """Extract and parse many invoice PDFs."""
    return [parse_invoice(path, **extract_kwargs) for path in paths]
