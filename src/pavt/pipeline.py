"""End-to-end batch orchestration, independent of the GUI.

``run_verification`` is the single entry point used by both the Streamlit app and
the command line, so the demo path and the automated tests exercise exactly the
same code.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .bank_file import BankFile, build_audit_report, build_bank_file
from .invoice_parser import parse_invoices
from .models import InvoiceDocument, MatchStatus, VerificationResult
from .money import format_amount, quantize
from .summary_reader import read_summary
from .verification import MatchOutcome, verify_batch


@dataclass(slots=True)
class BatchReport:
    """Everything produced by one verification run."""

    results: list[VerificationResult] = field(default_factory=list)
    invoices: list[InvoiceDocument] = field(default_factory=list)
    match_outcome: MatchOutcome | None = None
    warnings: list[str] = field(default_factory=list)
    bank_file: BankFile | None = None
    audit_report: BankFile | None = None
    generated_at: datetime | None = None

    @property
    def included(self) -> list[VerificationResult]:
        return [r for r in self.results if r.included_in_bank_file]

    @property
    def excluded(self) -> list[VerificationResult]:
        return [r for r in self.results if not r.included_in_bank_file]

    @property
    def total_payable(self) -> Decimal:
        return sum(
            (quantize(r.invoice_amount or r.request.amount) for r in self.included),
            Decimal("0.00"),
        )

    def counts_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {status.value: 0 for status in MatchStatus}
        for result in self.results:
            counts[result.status.value] += 1
        return counts


def run_verification(
    summary_source,
    invoice_sources,
    *,
    tolerance: Decimal = Decimal("0.01"),
    force_ocr: bool = False,
    ocr_engine=None,
    allow_vision: bool = True,
    generate_files: bool = True,
    batch_reference: str | None = None,
    generated_at: datetime | None = None,
) -> BatchReport:
    """Verify a batch and optionally build the bank file and audit report.

    Args:
        summary_source: Path to, or file-like object containing, the summary xlsx.
        invoice_sources: Iterable of invoice paths (PDF bytes are accepted as
            ``(name, bytes)`` pairs).
        tolerance: Maximum accepted difference between the requested and invoiced
            amounts, in HKD.
        force_ocr: Ignore PDF text layers and OCR everything.
        ocr_engine: Injectable OCR engine, for tests.
        allow_vision: Permit the optional cloud-vision fallback.
        generate_files: Build the bank file and audit report.
        batch_reference: Label for the generated files.
        generated_at: Timestamp override for deterministic output.

    Returns:
        A :class:`BatchReport`.
    """
    report = BatchReport(generated_at=generated_at or datetime.now())

    materialised = _materialise_invoices(invoice_sources, report)
    summary_path = _materialise_summary(summary_source, report)
    if summary_path is None:
        return report

    summary = read_summary(summary_path)
    report.warnings.extend(summary.warnings)
    report.warnings.extend(summary.skipped_rows)
    if not summary.requests:
        return report

    report.invoices = parse_invoices(
        materialised,
        force_ocr=force_ocr,
        ocr_engine=ocr_engine,
        allow_vision=allow_vision,
    )
    for invoice in report.invoices:
        report.warnings.extend(f"{invoice.filename}: {w}" for w in invoice.warnings)

    results, outcome = verify_batch(summary.requests, report.invoices, tolerance=tolerance)
    report.results = results
    report.match_outcome = outcome

    if outcome.unmatched_invoices:
        report.warnings.append(
            "Invoice(s) provided but not matched to any payment request: "
            + ", ".join(inv.filename for inv in outcome.unmatched_invoices)
        )

    if generate_files:
        report.bank_file = build_bank_file(
            results, batch_reference=batch_reference, generated_at=report.generated_at
        )
        report.audit_report = build_audit_report(results)

    return report


def _materialise_summary(source, report: BatchReport) -> Path | None:
    """Accept a path, a ``(name, bytes)`` pair or a file-like object."""
    if isinstance(source, (str, Path)):
        return Path(source)
    if isinstance(source, tuple) and len(source) == 2:
        name, data = source
        return _write_temp(name, data)
    if hasattr(source, "read"):
        name = Path(getattr(source, "name", "summary.xlsx")).name
        return _write_temp(name, source.read())
    report.warnings.append("Unsupported summary input; expected a path or uploaded file.")
    return None


def _materialise_invoices(sources, report: BatchReport) -> list[Path]:
    paths: list[Path] = []
    for source in sources or []:
        if isinstance(source, (str, Path)):
            paths.append(Path(source))
        elif isinstance(source, tuple) and len(source) == 2:
            name, data = source
            paths.append(_write_temp(name, data))
        elif hasattr(source, "read"):
            name = Path(getattr(source, "name", "invoice.pdf")).name
            paths.append(_write_temp(name, source.read()))
        else:
            report.warnings.append(f"Skipped unsupported invoice input: {source!r}")
    return paths


def _write_temp(name: str, data: bytes) -> Path:
    """Persist uploaded bytes so the extraction layer can treat them as files."""
    import tempfile

    suffix = Path(name).suffix or ".bin"
    handle = tempfile.NamedTemporaryFile(
        prefix=f"pavt_{Path(name).stem}_", suffix=suffix, delete=False
    )
    handle.write(data)
    handle.close()
    return Path(handle.name)


# --------------------------------------------------------------------------- #
# Command line interface
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pavt",
        description=(
            "Crosscheck a Payment Request Summary against invoice PDFs and generate "
            "a bank payment file."
        ),
    )
    parser.add_argument("summary", help="Payment Request Summary (.xlsx)")
    parser.add_argument(
        "invoices", nargs="+", help="Supporting document(s), e.g. invoice PDFs"
    )
    parser.add_argument(
        "-o", "--output-dir", default="output", help="Where to write generated files"
    )
    parser.add_argument(
        "-r", "--reference", default=None, help="Batch reference used in file names"
    )
    parser.add_argument(
        "--tolerance",
        default="0.01",
        help="Accepted amount difference in HKD (default: 0.01)",
    )
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="Ignore PDF text layers and OCR every document",
    )
    parser.add_argument(
        "--no-vision",
        action="store_true",
        help="Disable the optional cloud-vision OCR fallback",
    )
    parser.add_argument("--json", action="store_true", help="Print results as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    report = run_verification(
        args.summary,
        args.invoices,
        tolerance=Decimal(args.tolerance),
        force_ocr=args.force_ocr,
        allow_vision=not args.no_vision,
        batch_reference=args.reference,
    )

    output_dir = Path(args.output_dir)
    if report.bank_file:
        report.bank_file.write_to(output_dir / report.bank_file.filename)
    if report.audit_report:
        report.audit_report.write_to(output_dir / report.audit_report.filename)

    if args.json:
        print(
            json.dumps(
                {
                    "summary": str(args.summary),
                    "counts": report.counts_by_status(),
                    "total_payable": format_amount(report.total_payable),
                    "bank_file": str(output_dir / report.bank_file.filename)
                    if report.bank_file
                    else None,
                    "rows": [
                        {
                            "request_no": r.request.request_no,
                            "payee": r.request.payee_name,
                            "requested": format_amount(r.request.amount),
                            "invoice": r.invoice.filename if r.invoice else None,
                            "invoice_amount": format_amount(r.invoice_amount),
                            "status": r.status.value,
                            "source": r.invoice.source_kind.value if r.invoice else None,
                            "remark": r.note,
                        }
                        for r in report.results
                    ],
                    "warnings": report.warnings,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print("Payment verification complete")
        print("-" * 72)
        for result in report.results:
            marker = "PASS" if result.included_in_bank_file else "HOLD"
            invoice = result.invoice.filename if result.invoice else "(none)"
            print(
                f"[{marker}] {result.request.request_no:<10} {result.request.payee_name:<32} "
                f"requested {format_amount(result.request.amount):>12}  "
                f"invoice {format_amount(result.invoice_amount):>12}  {result.status.value}"
            )
            print(f"        invoice file: {invoice}")
        print("-" * 72)
        print(f"Included in bank file : {len(report.included)}")
        print(f"Excluded              : {len(report.excluded)}")
        print(f"Total payable (HKD)   : {format_amount(report.total_payable)}")
        if report.bank_file:
            print(f"Bank file             : {output_dir / report.bank_file.filename}")
        if report.audit_report:
            print(f"Audit report          : {output_dir / report.audit_report.filename}")
        for warning in report.warnings:
            print(f"[warning] {warning}")

    return 0 if report.results else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
