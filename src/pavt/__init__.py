"""Payment Automation & Verification Tool.

An AI-assisted workflow that crosschecks the ``Payment Amount`` in a Payment
Request Summary (Excel) against the totals on the matching supporting documents
(PDF invoices), then emits a bank upload file containing only verified payments.

Public entry points
-------------------
``pavt.pipeline.run_verification``
    End-to-end, GUI-independent verification of a batch.
``pavt.extraction``
    PDF text-layer extraction, offline OCR and optional cloud-vision OCR.
``pavt.invoice_parser``
    Template-independent invoice total/field parsing.
``pavt.bank_file``
    Bank upload file (Excel) generation.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
