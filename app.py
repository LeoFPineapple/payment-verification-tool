"""Streamlit GUI for the Payment Automation & Verification Tool.

Run with::

    streamlit run app.py

The screen follows the three steps an accounts-payable operator actually performs:

1. **Upload** -- drop in the Payment Request Summary and any number of invoices.
2. **Verify** -- the tool extracts each invoice amount (text layer, or offline OCR
   for scans) and crosschecks it against the summary.
3. **Download** -- the bank payment file is generated and downloaded automatically;
   held-back payments are explained on screen.
"""

from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import streamlit as st

# Make both the package and any vendored OCR wheels importable.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from pavt.bootstrap import bootstrap, vendor_dir  # noqa: E402

VENDORED = bootstrap()

from pavt.bank_file import build_audit_report, build_bank_file  # noqa: E402
from pavt.models import MatchStatus  # noqa: E402
from pavt.money import format_amount, format_amount_display  # noqa: E402
from pavt.pipeline import run_verification  # noqa: E402
from pavt.vision import vision_enabled  # noqa: E402

STATUS_BADGE = {
    MatchStatus.MATCHED: ("PASS", "normal"),
    MatchStatus.AMOUNT_MISMATCH: ("HOLD", "inverse"),
    MatchStatus.NO_INVOICE: ("HOLD", "inverse"),
    MatchStatus.UNREADABLE: ("HOLD", "off"),
}

SOURCE_LABEL = {
    "TEXT_LAYER": "PDF text layer",
    "OCR_LOCAL": "Offline OCR",
    "OCR_VISION": "Cloud vision OCR",
    "NONE": "-",
}


def _init_state() -> None:
    for key, value in {
        "report": None,
        "uploader_key": 0,
        "new_run": False,
    }.items():
        st.session_state.setdefault(key, value)


def _render_sidebar() -> dict:
    with st.sidebar:
        st.header("Settings")
        tolerance_text = st.text_input(
            "Amount tolerance (HKD)",
            value="0.01",
            help=(
                "Maximum difference treated as a match. 0.01 tolerates one cent of "
                "rounding; raise it only if your invoices legitimately round."
            ),
        )
        force_ocr = st.checkbox(
            "Force OCR (skip PDF text layer)",
            value=False,
            help="Use this to demonstrate the OCR route on digitally generated PDFs.",
        )
        st.divider()
        st.subheader("Document status")
        st.caption(
            f"Offline OCR: **{'available' if VENDORED else 'via installed packages'}**  \n"
            f"Cloud vision fallback: **{'configured' if vision_enabled() else 'not configured'}**"
        )
        if not VENDORED:
            st.caption(f"Vendored wheels not found at `{vendor_dir()}`")
        st.divider()
        st.subheader("How amounts are read")
        st.markdown(
            "1. **PDF text layer** - instant, exact\n"
            "2. **Offline OCR** (RapidOCR/ONNX) - for scans, no network\n"
            "3. **Cloud vision** - optional fallback"
        )
    try:
        tolerance = Decimal(tolerance_text)
        if tolerance < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        st.sidebar.error("Tolerance must be a non-negative number; using 0.01.")
        tolerance = Decimal("0.01")
    return {"tolerance": tolerance, "force_ocr": force_ocr}


def _render_uploads() -> tuple[list, list]:
    left, right = st.columns(2)
    with left:
        st.subheader("1. Payment Request Summary")
        summary_files = st.file_uploader(
            "Excel summary (.xlsx)",
            type=["xlsx", "xlsm"],
            accept_multiple_files=False,
            key=f"summary_{st.session_state.uploader_key}",
            help="Contains Payment Request No., Payee Name, Bank Code, Account Number, Particulars and Payment Amount.",
        )
    with right:
        st.subheader("2. Payment supporting documents")
        invoice_files = st.file_uploader(
            "Invoice PDFs (multiple allowed)",
            type=["pdf"],
            accept_multiple_files=True,
            key=f"invoices_{st.session_state.uploader_key}",
            help="Scanned or digital invoices. Multiple files are supported in one upload.",
        )
    return ([summary_files] if summary_files else []), list(invoice_files or [])


def _render_results(report) -> None:
    bank = report.bank_file
    included, excluded = report.included, report.excluded

    st.subheader("3. Verification result")
    metrics = st.columns(4)
    metrics[0].metric("Requests checked", len(report.results))
    metrics[1].metric("Included in bank file", len(included))
    metrics[2].metric("Held back", len(excluded))
    metrics[3].metric("Total payable (HKD)", format_amount_display(report.total_payable))

    if bank and bank.row_count:
        st.success(
            f"Bank payment file generated with {bank.row_count} payment(s). "
            "Use the download button below."
        )
    else:
        st.error("No payment passed verification, so no bank file rows were produced.")

    st.markdown("#### Crosscheck detail")
    st.dataframe(
        [
            {
                "": STATUS_BADGE[result.status][0],
                "Payment Request No": result.request.request_no,
                "Payee Name": result.request.payee_name,
                "Requested (HKD)": format_amount_display(result.request.amount),
                "Invoice File": result.invoice.filename if result.invoice else "-",
                "Invoice Amount (HKD)": format_amount_display(result.invoice_amount),
                "Difference (HKD)": (
                    f"{result.difference:+,.2f}" if result.difference is not None else "-"
                ),
                "Status": result.status.value,
                "Read via": (
                    SOURCE_LABEL.get(result.invoice.source_kind.value, "-")
                    if result.invoice
                    else "-"
                ),
                "Remark": result.note,
            }
            for result in report.results
        ],
        width="stretch",
        hide_index=True,
    )
    if excluded:
        with st.expander(f"Held back payments ({len(excluded)})", expanded=True):
            for result in excluded:
                st.markdown(f"**{result.request.request_no} - {result.request.payee_name}**")
                st.caption(result.note)
                if result.match_reason:
                    st.caption(f"Match basis: {result.match_reason}")

    if report.warnings:
        with st.expander(f"Warnings and notes ({len(report.warnings)})"):
            for warning in report.warnings:
                st.write(f"- {warning}")

    st.markdown("#### Downloads")
    cols = st.columns(2)
    if bank:
        cols[0].download_button(
            "Download bank payment file (.xlsx)",
            data=bank.content,
            file_name=bank.filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            width="stretch",
        )
    audit = report.audit_report or build_audit_report(report.results)
    cols[1].download_button(
        "Download audit report (.xlsx)",
        data=audit.content,
        file_name=audit.filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )

    st.caption(
        "The bank payment file is generated automatically after verification; use the "
        "download button to save it. The audit report lists every request, including "
        "the ones held back and why."
    )


def main() -> None:
    st.set_page_config(
        page_title="Payment Automation & Verification Tool",
        page_icon="🧾",
        layout="wide",
    )
    _init_state()

    st.title("Payment Automation & Verification Tool")
    st.caption(
        "Upload a Payment Request Summary and its supporting invoices. The tool reads "
        "each invoice amount with OCR and crosschecks it against the summary before "
        "generating the bank payment file."
    )

    settings = _render_sidebar()
    st.divider()
    summaries, invoices = _render_uploads()
    st.divider()

    run = st.button(
        "Verify payments and generate bank file",
        type="primary",
        disabled=not (summaries and invoices),
    )
    if not summaries:
        st.info("Upload the Payment Request Summary (Excel) to begin.")
    elif not invoices:
        st.info("Upload at least one supporting document (invoice PDF).")

    if run:
        if not (summaries and invoices):
            st.warning("Both a summary and at least one invoice are required.")
        else:
            with st.spinner(
                "Reading documents and crosschecking amounts... "
                "The first OCR run loads the recognition models, so it can take a moment."
            ):
                report = run_verification(
                    (summaries[0].name, summaries[0].getvalue()),
                    [(f.name, f.getvalue()) for f in invoices],
                    tolerance=settings["tolerance"],
                    force_ocr=settings["force_ocr"],
                    batch_reference=f"PAYMENT-{datetime.now():%Y%m%d-%H%M%S}",
                )
            st.session_state.report = report
            st.session_state.new_run = True

    if st.session_state.report is not None:
        _render_results(st.session_state.report)
        if st.session_state.new_run:
            # Clicking a Streamlit download button cannot be scripted from inside
            # the app's sandboxed frame, so the file is surfaced prominently
            # instead of silently: the button is rendered with type="primary" and
            # this callout tells the operator it is ready.
            st.session_state.new_run = False
            st.toast("Bank payment file ready - use the download button below.", icon="✅")

    with st.expander("Start a new batch"):
        if st.button("Clear uploaded files and results"):
            st.session_state.report = None
            st.session_state.uploader_key += 1
            st.rerun()


if __name__ == "__main__":
    main()
