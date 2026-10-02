"""Document text acquisition: PDF text layer, offline OCR, optional cloud vision.

The ordering is deliberate and cheapest-first:

1. **PDF text layer** (PyMuPDF).  Instant and perfectly accurate, but only exists
   on digitally generated PDFs.  Sample file ``P0003.pdf`` takes this path.
2. **Offline OCR** (RapidOCR / ONNX).  Needed for scans such as ``P0001.pdf`` and
   ``P0002.pdf``, which are page images with no useful text layer.  Handles
   traditional Chinese and English, runs with no network and no API key.
3. **Cloud vision model** (optional).  Only used when configured *and* the local
   paths failed or ``PAVT_OCR_MODE=vision`` is set; see :mod:`pavt.vision`.

Every result records which route produced it, so the GUI and the audit report can
show the operator exactly how a figure was obtained.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .models import SourceKind

# Rendering resolution for OCR.  300 DPI is the sweet spot for invoice tables:
# high enough for 8pt font, still fast on CPU.
DEFAULT_DPI = int(os.environ.get("PAVT_OCR_DPI", "300"))

# Below this many extracted characters a text layer is treated as absent.  The
# scanned samples yield only the letterhead ("ABC Medical Centre Limited"), so a
# naive truthiness check would wrongly accept them as complete.
MIN_TEXT_LAYER_CHARS = int(os.environ.get("PAVT_MIN_TEXT_LAYER_CHARS", "60"))


@dataclass(slots=True)
class ExtractionResult:
    """Raw text plus provenance for one document."""

    text: str
    source_kind: SourceKind
    page_count: int = 0
    engine: str = ""
    warnings: list[str] | None = None
    page_images: list[bytes] | None = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


# --------------------------------------------------------------------------- #
# PDF rasterisation
# --------------------------------------------------------------------------- #


def render_pdf_pages(data: bytes, dpi: int = DEFAULT_DPI) -> list[bytes]:
    """Rasterise every page of ``data`` to PNG bytes."""
    import pymupdf  # imported lazily: keeps pure-text runs dependency-light

    pages: list[bytes] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            pixmap = page.get_pixmap(dpi=dpi)
            pages.append(pixmap.tobytes("png"))
    return pages


def extract_pdf_text_layer(data: bytes) -> tuple[str, int]:
    """Return ``(text, page_count)`` from the PDF text layer."""
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
        return text, doc.page_count


def looks_like_text_layer(text: str) -> bool:
    """True when the extracted text is substantial enough to trust on its own."""
    stripped = re.sub(r"\s+", "", text or "")
    if len(stripped) < MIN_TEXT_LAYER_CHARS:
        return False
    # A single repeated letterhead still counts as "no text layer".
    return len(set(stripped)) > 8


# --------------------------------------------------------------------------- #
# OCR engines
# --------------------------------------------------------------------------- #


class OcrUnavailableError(RuntimeError):
    """Raised when no OCR engine can be loaded."""


class RapidOcrEngine:
    """Offline OCR via RapidOCR (PP-OCR models on ONNX Runtime)."""

    name = "RapidOCR (ONNX, offline)"

    def __init__(self) -> None:
        from .bootstrap import bootstrap

        bootstrap()  # no-op unless wheels were vendored into .pylibs/
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise OcrUnavailableError(
                "RapidOCR is not installed. Run 'pip install -r requirements.txt'."
            ) from exc
        self._engine = RapidOCR()

    def image_to_text(self, image_bytes: bytes) -> str:
        """Run OCR over one page image and rebuild the page as text lines."""
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            array = np.array(img.convert("RGB"))

        result, _elapse = self._engine(array)
        if not result:
            return ""

        boxes: list[tuple[float, float, float, float, str]] = []
        for box, text, _score in result:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            boxes.append((min(ys), max(ys), min(xs), max(xs), str(text)))

        return group_boxes_into_lines(boxes)


def group_boxes_into_lines(
    boxes: list[tuple[float, float, float, float, str]],
    *,
    vertical_overlap_ratio: float = 0.5,
    horizontal_gap: float = 24.0,
) -> str:
    """Rebuild visual text rows from OCR word boxes.

    OCR returns boxes in an arbitrary order, but the invoice parser relies on a
    label being near its value.  Sorting by ``y // constant`` looks reasonable yet
    fails exactly where it matters: a label at y=482 and its value at y=500 can
    land in different buckets, splitting the row apart.

    Instead, boxes are clustered into rows by vertical overlap, then every cluster
    is ordered by ``x``; boxes that sit on the same row and are horizontally close
    are joined with a space, which restores the original reading order.
    """
    if not boxes:
        return ""

    ordered = sorted(boxes, key=lambda box: (box[0], box[2]))
    rows: list[list[tuple[float, float, float, float, str]]] = []

    for box in ordered:
        top, bottom, _left, _right, _text = box
        height = max(bottom - top, 1.0)
        placed = False
        for row in rows:
            row_top = min(item[0] for item in row)
            row_bottom = max(item[1] for item in row)
            row_height = max(row_bottom - row_top, 1.0)
            overlap = min(bottom, row_bottom) - max(top, row_top)
            if overlap > 0 and overlap / min(height, row_height) >= vertical_overlap_ratio:
                row.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])

    lines: list[str] = []
    for row in sorted(rows, key=lambda items: min(item[0] for item in items)):
        items = sorted(row, key=lambda item: item[2])
        parts: list[str] = []
        previous_right: float | None = None
        for _top, _bottom, left, right, text in items:
            if text:
                if previous_right is not None:
                    if left - previous_right > horizontal_gap:
                        parts.append("  ")  # a clear column break
                    elif left > previous_right:
                        parts.append(" ")  # merely adjacent words
                parts.append(text)
            previous_right = right
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def get_offline_ocr_engine(engine: RapidOcrEngine | None = None):
    """Return a cached offline OCR engine, or raise :class:`OcrUnavailableError`."""
    global _CACHED_ENGINE
    if engine is not None:
        return engine
    if _CACHED_ENGINE is None:
        _CACHED_ENGINE = RapidOcrEngine()
    return _CACHED_ENGINE


_CACHED_ENGINE = None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def extract_document(
    path: str | Path,
    *,
    ocr_engine=None,
    force_ocr: bool = False,
    allow_vision: bool = True,
    dpi: int = DEFAULT_DPI,
) -> ExtractionResult:
    """Extract text from a PDF, choosing the cheapest reliable route.

    Args:
        path: PDF file to read.
        ocr_engine: Injectable engine (a :class:`RapidOcrEngine`-like object) so
            tests can run without ONNX models.
        force_ocr: Skip the text layer, for demonstrating the OCR path.
        allow_vision: Permit the optional cloud-vision fallback.
        dpi: Rasterisation resolution for OCR.
    """
    path = Path(path)
    data = path.read_bytes()
    warnings: list[str] = []

    pages: list[bytes] = []
    page_count = 0
    if not force_ocr:
        try:
            text, page_count = extract_pdf_text_layer(data)
        except Exception as exc:  # noqa: BLE001 - corrupt or encrypted PDF
            warnings.append(f"Text-layer extraction failed: {exc}")
            text = ""
        if looks_like_text_layer(text):
            return ExtractionResult(
                text=text,
                source_kind=SourceKind.TEXT_LAYER,
                page_count=page_count,
                engine="PDF text layer (PyMuPDF)",
                warnings=warnings,
            )
        warnings.append(
            "No usable text layer found; document is a scan, OCR required."
        )
    else:
        warnings.append("Text layer skipped (OCR forced).")

    try:
        pages = render_pdf_pages(data, dpi=dpi)
        page_count = page_count or len(pages)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not rasterise PDF: {exc}")

    ocr_text = ""
    engine_name = ""
    if pages:
        try:
            engine = get_offline_ocr_engine(ocr_engine)
            engine_name = getattr(engine, "name", engine.__class__.__name__)
            ocr_text = "\n".join(engine.image_to_text(page) for page in pages)
        except OcrUnavailableError as exc:
            warnings.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Offline OCR failed: {exc}")

    if ocr_text.strip():
        # The "scan, OCR required" breadcrumb is not an operator-facing problem:
        # OCR recovered the document, so the note is dropped from the result.
        return ExtractionResult(
            text=ocr_text,
            source_kind=SourceKind.OCR_LOCAL,
            page_count=page_count,
            engine=engine_name,
            warnings=[w for w in warnings if not w.startswith("No usable text layer")],
            page_images=pages,
        )

    if allow_vision:
        from .vision import vision_ocr

        vision_text, vision_warning = vision_ocr(pages or data)
        if vision_warning:
            warnings.append(vision_warning)
        if vision_text.strip():
            return ExtractionResult(
                text=vision_text,
                source_kind=SourceKind.OCR_VISION,
                page_count=page_count,
                engine="Cloud vision model",
                warnings=warnings,
                page_images=pages,
            )

    return ExtractionResult(
        text="",
        source_kind=SourceKind.NONE,
        page_count=page_count,
        engine="",
        warnings=warnings,
        page_images=pages,
    )
