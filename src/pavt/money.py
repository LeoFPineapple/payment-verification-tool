"""Money parsing / formatting and text-normalisation helpers.

Invoice amounts arrive as OCR text, so parsing has to be defensive: it must read
``15,000.00``, ``HKD 15,000.00``, ``$120000.00`` and ``1,230`` correctly while
never mistaking an invoice number (``140981024``), a date (``2026/09/08``) or a
bank account (``456789123``) for an amount.

The governing rule: **a figure is only money when the document marks it as money**
-- it carries a decimal point, or a thousands separator, or a currency cue.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

TWO_PLACES = Decimal("0.01")


def to_decimal(value: object) -> Decimal | None:
    """Convert ``value`` to ``Decimal`` without binary-float drift, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Decimal(float) would carry the binary artefact, so go via the repr.
        return Decimal(repr(value))
    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[,\s]", "", text)
    cleaned = re.sub(r"^[^\d.\-+]+", "", cleaned)  # drop leading "HKD", "$", ...
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def quantize(amount: Decimal) -> Decimal:
    """Round to cents, half-up, the way an accountant expects."""
    return amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def format_amount(amount: Decimal | None) -> str:
    """Render an amount for a report cell, e.g. ``120000.00``."""
    if amount is None:
        return ""
    return f"{quantize(amount):f}"


def format_amount_display(amount: Decimal | None) -> str:
    """Render an amount for humans, e.g. ``120,000.00``."""
    if amount is None:
        return ""
    return f"{quantize(amount):,.2f}"


def amounts_equal(left: Decimal, right: Decimal, tolerance: Decimal) -> bool:
    """True when two amounts agree within the configured tolerance."""
    return abs(quantize(left) - quantize(right)) <= tolerance


# --------------------------------------------------------------------------- #
# Amount extraction
# --------------------------------------------------------------------------- #

# OCR frequently confuses the letter O with zero inside a number: the older
# PP-OCRv3 model reads "15,000.00" as "15,ooo.o0" and "5,800.00" as "5,8oo.oo".
#
# Repair works on whole numeric tokens rather than on individual letter runs,
# because a run's meaning depends on the token around it and a per-run regex gets
# its boundaries wrong on "15,ooo.o0" (it fixes the first run, then misreads the
# second).  This mirrors the way the amount scanner itself sees the text.
_OCR_NUMBER_TOKEN = re.compile(r"\d(?:[\d.,]*[Oo][\d.,]*)+")

# A digit run where a period was used where a thousands separator belongs, as in
# "15.000.00" (really 15,000.00).  Normalised in the text before tokenising,
# because the amount regex would otherwise match only the tail ("000.00").
_MISREAD_GROUPING = re.compile(r"\b(\d{1,3})\.(\d{3})\.(\d{2})\b")


def _repair_token(token: str) -> str:
    """Convert letter-O digits to zeroes inside one numeric token.

    Only two positions count as digits, which keeps product codes such as
    ``ABCO123`` intact:

    * an ``O``/``o`` run followed by a digit or separator, i.e. inside the number
      (the ``oo`` in ``5,8oo.oo``);
    * a run at the very end of a token that already has a true digit before its
      final separator, i.e. the cents of ``15,ooo.oo``.
    """
    chars = list(token)
    index = 0
    while index < len(chars):
        if chars[index] not in "Oo":
            index += 1
            continue

        run_end = index
        while run_end < len(chars) and chars[run_end] in "Oo":
            run_end += 1

        after = chars[run_end] if run_end < len(chars) else ""
        if after and (after.isdigit() or after in ".,"):
            is_digit_run = True
        elif not after and "." in token:
            # Trailing run: accept when a real digit already appears before the
            # final separator, so "15,ooo.oo" is repaired but "ABCO" is not.
            is_digit_run = any(ch.isdigit() for ch in token[: token.rindex(".")])
        else:
            is_digit_run = False

        if is_digit_run:
            for position in range(index, run_end):
                chars[position] = "0"
        index = run_end

    return "".join(chars)


def normalise_ocr_numbers(text: str) -> str:
    """Repair the numeric misreads our OCR models actually produce.

    Applied to the raw text *before* amounts are tokenised: doing it afterwards is
    too late, because the amount regex has already fixed the match boundaries on
    the damaged text.

    Two conservative repairs are made, both inside numeric tokens only:

    * letter ``O``/``o`` read where a zero belongs (``15,ooo.o0`` -> ``15,000.00``);
    * a period used where a thousands separator belongs (``15.000.00`` ->
      ``15,000.00``).

    Anything else is left untouched: the parser must never invent an amount that is
    not on the page.
    """
    if not text:
        return text

    repaired = _OCR_NUMBER_TOKEN.sub(lambda match: _repair_token(match.group(0)), text)
    return _MISREAD_GROUPING.sub(
        lambda match: f"{match.group(1)},{match.group(2)}.{match.group(3)}", repaired
    )

# A money token: optional currency cue, digits with optional thousands grouping,
# and a mandatory decimal part -- the decimal part is what proves "this is money".
#
# The trailing `(?![\d.])` guard is essential: without it "1.23456" would match as
# "1.23", inventing a cash amount out of an exchange rate or a unit price.
_MONEY_TOKEN = re.compile(
    r"""
    (?P<currency>HKD|HK\$|USD|CNY|RMB|EUR|\$|¥|￥)?
    \s*
    (?P<number>
        \d{1,3}(?:,\d{3})+(?:\.\d{1,2})?     # 1,230  |  120,000.00
      | \d+\.\d{1,2}                          # 1230.00
      | \d{1,3}(?:,\d{3})+                    # 1,230 (bare, grouped)
      | \d+(?:\.\d+)?                         # 1230 or 12.5 -- weakest, see below
    )
    (?![\d.])
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Numbers that are almost never the invoice total.
_NOISE_PATTERNS = (
    re.compile(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$"),  # ISO-ish date
    re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$"),  # d/m/y
    re.compile(r"^\d{6,}$"),  # long digit run, e.g. invoice no. or account no.
)

# Lines whose amount is the document total. Order of the alternatives matters:
# Chinese cues first, then the English ones, matching the sample invoices.
_TOTAL_LINE = re.compile(
    r"(?:"
    r"總\s*計|合\s*計|總\s*額|應\s*付"          # 總計 / 合計 / 總額 / 應付
    r"|total\s+amount|grand\s+total|amount\s+due|total\s+due|balance\s+due"
    r"|total"
    r")",
    re.IGNORECASE,
)

# Row-label cues that mark a *line item* rather than a document total.
_LINE_ITEM_CUE = re.compile(r"sub\s*-?\s*total|小\s*計", re.IGNORECASE)


def _classify_token(raw_number: str, currency: str | None) -> Decimal | None:
    """Turn a matched number into a Decimal, or None when it cannot be money."""
    digits_only = raw_number.replace(",", "")
    has_decimal = "." in raw_number
    has_grouping = "," in raw_number

    if any(p.match(digits_only) for p in _NOISE_PATTERNS) and not has_decimal:
        # A 9-digit run with no decimal point is an id/account, not an amount --
        # unless the document explicitly prefixed it with a currency.
        if not currency:
            return None

    if not (has_decimal or has_grouping or currency):
        # A bare integer is too ambiguous to treat as money on its own.
        return None

    if has_decimal:
        integer_part, _, frac = raw_number.partition(".")
        if len(frac) > 2:
            return None  # more precision than cents: not a cash amount
        if len(integer_part.replace(",", "")) > 12:
            return None

    try:
        return Decimal(digits_only)
    except InvalidOperation:
        return None


def find_amounts(text: str) -> list[tuple[Decimal, str, bool]]:
    """Extract candidate amounts from ``text``.

    Returns ``(amount, matched_text, had_currency_cue)`` triples in reading order.
    Known OCR numeric misreads are repaired first (see :func:`normalise_ocr_numbers`).
    """
    found: list[tuple[Decimal, str, bool]] = []
    for match in _MONEY_TOKEN.finditer(normalise_ocr_numbers(text)):
        raw_number = match.group("number")
        currency = match.group("currency")
        amount = _classify_token(raw_number, currency)
        if amount is None:
            continue
        found.append((amount, match.group(0).strip(), bool(currency)))
    return found


def extract_total_amount(text: str) -> Decimal | None:
    """Best-effort extraction of an invoice's payable total.

    Strategy (highest confidence first):

    1. the figure on a line carrying an explicit total cue (``TOTAL``, ``總計``,
       ``Amount Due`` ...) -- the *last* such figure wins, because totals are
       printed at the bottom;
    2. otherwise the largest candidate amount in the document;
    3. otherwise the last candidate amount.

    Returns ``None`` when the document contains no money-like figure at all.
    """
    if not text or not text.strip():
        return None

    total_candidates: list[Decimal] = []
    for line in text.splitlines():
        bare = line.strip()
        if not bare or not _TOTAL_LINE.search(bare):
            continue
        if _LINE_ITEM_CUE.search(bare):  # a 小計/Sub-total row is not the total
            continue
        line_amounts = [amount for amount, _, _ in find_amounts(bare)]
        if line_amounts:
            # "Total Amount: HKD 15,000.00" -> the last figure after the cue.
            total_candidates.extend(line_amounts)

    if total_candidates:
        return quantize(total_candidates[-1])

    all_amounts = [amount for amount, _, _ in find_amounts(text)]
    if not all_amounts:
        return None
    if len(all_amounts) == 1:
        return quantize(all_amounts[0])
    # No explicit total row: the payable figure is normally the largest one.
    largest = max(all_amounts)
    return quantize(largest)


# --------------------------------------------------------------------------- #
# Text normalisation for matching
# --------------------------------------------------------------------------- #

_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def normalise_name(value: str | None) -> str:
    """Fold a payee name for comparison.

    Case, spacing, punctuation and the Limited/Ltd style abbreviations are all
    removed so that OCR noise such as ``ABCMedical CentreLimited`` still matches
    ``ABC Medical Centre Limited``.
    """
    if not value:
        return ""
    text = value.casefold()
    text = text.replace("（", "(").replace("）", ")")
    text = _PUNCT.sub("", text)
    for long_form, short_form in (
        ("limited", "ltd"),
        ("company", "co"),
        ("corporation", "corp"),
        ("incorporated", "inc"),
    ):
        text = text.replace(long_form, short_form)
    return text


def normalise_digits(value: str | None) -> str:
    """Keep digits only -- used for bank code / account comparison."""
    if not value:
        return ""
    return re.sub(r"\D", "", str(value))
