"""Render the architecture and workflow diagram to ``docs/architecture.png``.

Kept as code so the diagram can be regenerated after a design change rather than
drifting out of date as a hand-drawn image::

    python tools/make_diagram.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "architecture.png"

# The diagram labels a language-agnostic parser as "EN / 繁中", so a CJK-capable
# font is required; matplotlib's default DejaVu Sans has no Chinese glyphs.
_FONT_CANDIDATES = (
    "Microsoft JhengHei",  # traditional Chinese, ships with Windows
    "Microsoft YaHei",
    "Noto Sans CJK TC",
    "SimHei",
    "Arial Unicode MS",
)
_available = {font.name for font in matplotlib.font_manager.fontManager.ttflist}
for _candidate in _FONT_CANDIDATES:
    if _candidate in _available:
        plt.rcParams["font.family"] = _candidate
        break
else:  # pragma: no cover - only on a machine with no CJK font installed
    plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

INK = "#1f2933"
EDGE = "#52606d"
INPUT = "#dbeafe"      # inputs and outputs
PROCESS = "#e6f4ea"    # deterministic steps
AI = "#fde9d9"         # AI / OCR steps
DECIDE = "#fff4cc"     # decision
PASS = "#d4edda"       # verified
HOLD = "#f8d7da"       # held back
GUI = "#f3e8ff"


def box(ax, x, y, w, h, text, color, *, fontsize=9, bold=False):
    ax.add_patch(
        mpatches.FancyBboxPatch(
            (x, y), w, h,
            boxstyle="round,pad=0.006,rounding_size=0.022",
            linewidth=1.2, edgecolor=EDGE, facecolor=color, zorder=2,
        )
    )
    ax.text(
        x + w / 2, y + h / 2, text,
        ha="center", va="center", fontsize=fontsize, color=INK,
        fontweight="bold" if bold else "normal", zorder=3, linespacing=1.5,
    )


def arrow(ax, start, end, *, color=EDGE, rad=0.0, style="->", lw=1.3):
    ax.annotate(
        "", xy=end, xytext=start,
        arrowprops=dict(arrowstyle=style, color=color, linewidth=lw,
                        connectionstyle=f"arc3,rad={rad}", shrinkA=2, shrinkB=2),
        zorder=1,
    )


def note(ax, x, y, text, *, color=EDGE, fontsize=8, ha="center", weight="normal"):
    ax.text(x, y, text, ha=ha, va="center", fontsize=fontsize, color=color,
            fontweight=weight, zorder=5)


def main() -> int:
    fig, ax = plt.subplots(figsize=(14.0, 9.6), dpi=150)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    note(ax, 0.5, 0.975, "Payment Automation & Verification Tool - architecture",
         fontsize=15, weight="bold")
    note(ax, 0.5, 0.943,
         "AI-driven crosscheck of Payment Request Summary amounts against invoice PDFs",
         fontsize=9.5)

    # ---------------------------------------------------------------- inputs --
    box(ax, 0.025, 0.815, 0.185, 0.082, "Payment Request Summary\n(.xlsx)", INPUT, bold=True)
    box(ax, 0.025, 0.700, 0.185, 0.082,
        "Supporting documents\n(invoice PDFs, multi-upload)", INPUT, bold=True)

    # ------------------------------------------------------- step 1: summary --
    box(ax, 0.262, 0.815, 0.205, 0.082,
        "1. Summary reader\nheader cues -> normalised rows\n(Decimal amounts)", PROCESS)
    arrow(ax, (0.210, 0.856), (0.262, 0.856))

    # -------------------------------------------- step 2: text acquisition --
    box(ax, 0.262, 0.575, 0.205, 0.108,
        "2. Text acquisition\ncheapest route first", PROCESS, bold=True)
    arrow(ax, (0.210, 0.741), (0.262, 0.678), rad=-0.10)

    route_x, route_w = 0.545, 0.215
    box(ax, route_x, 0.800, route_w, 0.066, "PDF text layer (PyMuPDF)", PROCESS)
    box(ax, route_x, 0.718, route_w, 0.066, "Offline OCR\nRapidOCR / PP-OCR on ONNX", AI, fontsize=8.5)
    box(ax, route_x, 0.636, route_w, 0.066, "Cloud vision model\n(optional fallback)", AI, fontsize=8.5)
    arrow(ax, (0.467, 0.655), (route_x, 0.833), rad=-0.12)
    arrow(ax, (0.467, 0.630), (route_x, 0.751))
    arrow(ax, (0.467, 0.605), (route_x, 0.669), rad=0.12)
    note(ax, 0.508, 0.862, "text layer\nusable?", fontsize=7)
    note(ax, 0.508, 0.760, "no", fontsize=7)
    note(ax, 0.508, 0.598, "still\nnothing", fontsize=7)

    # -------------------------------------------------- step 3: invoice parse --
    box(ax, 0.820, 0.718, 0.155, 0.148,
        "3. Invoice parser\nlabel-intent cues\n   (EN / 繁中)\n-> TOTAL amount",
        PROCESS, bold=True, fontsize=8.5)
    arrow(ax, (route_x + route_w, 0.833), (0.820, 0.812), rad=-0.10)
    arrow(ax, (route_x + route_w, 0.751), (0.820, 0.786))
    arrow(ax, (route_x + route_w, 0.669), (0.820, 0.760), rad=0.10)

    # ---------------------------------------------------- step 4: matching ----
    box(ax, 0.262, 0.395, 0.325, 0.100,
        "4. Matching engine\npayee name + bank account evidence,\nmost-confident pairing first",
        PROCESS, bold=True, fontsize=9)
    arrow(ax, (0.300, 0.815), (0.340, 0.495), rad=0.08)
    arrow(ax, (0.898, 0.718), (0.587, 0.470), rad=-0.12)
    note(ax, 0.905, 0.700, "invoice total", fontsize=7.5)

    # -------------------------------------------------- step 5: crosscheck ----
    box(ax, 0.660, 0.395, 0.315, 0.100,
        "5. Amount crosscheck\n| requested - invoiced | <= tolerance",
        DECIDE, bold=True, fontsize=9.5)
    arrow(ax, (0.587, 0.445), (0.660, 0.445))

    # ------------------------------------------------------- outcomes ---------
    box(ax, 0.660, 0.268, 0.315, 0.072,
        "MATCHED  ->  payment batch", PASS, bold=True, fontsize=9)
    box(ax, 0.660, 0.150, 0.315, 0.082,
        "AMOUNT_MISMATCH / NO_INVOICE /\nUNREADABLE  ->  held back", HOLD,
        bold=True, fontsize=9)
    arrow(ax, (0.8175, 0.395), (0.8175, 0.340))
    note(ax, 0.828, 0.3675, "equal", fontsize=7.5, ha="left")
    arrow(ax, (0.760, 0.395), (0.760, 0.232))
    note(ax, 0.756, 0.340, "not equal", fontsize=7.5, ha="right")

    # -------------------------------------------------------- outputs ---------
    box(ax, 0.180, 0.268, 0.280, 0.090,
        "6. Bank payment file (.xlsx)\nPayee Name | Bank Code + Account |\nPayment Amount (HKD)",
        INPUT, bold=True, fontsize=8.5)
    box(ax, 0.180, 0.130, 0.280, 0.078,
        "Audit report (.xlsx)\nevery request, its verdict and why",
        INPUT, fontsize=8.5)
    arrow(ax, (0.660, 0.304), (0.460, 0.313), rad=0.10)
    arrow(ax, (0.660, 0.191), (0.460, 0.176), rad=0.08)
    arrow(ax, (0.320, 0.268), (0.320, 0.208))
    note(ax, 0.332, 0.238, "styles: header, totals, number format", fontsize=7, ha="left")

    # ------------------------------------------------------- GUI on the left --
    box(ax, 0.025, 0.300, 0.185, 0.230,
        "Streamlit GUI\n\nmulti-file upload\nrun verification\n"
        "review PASS / HOLD\ndownload results",
        GUI, bold=True, fontsize=9)
    arrow(ax, (0.210, 0.440), (0.262, 0.445), rad=0.0)
    note(ax, 0.236, 0.478, "same\npipeline\ncall", fontsize=7)

    # ------------------------------------------------------------ legend ------
    legend = [
        (INPUT, "input / output"),
        (PROCESS, "deterministic step"),
        (AI, "AI / OCR step"),
        (DECIDE, "decision"),
        (HOLD, "excluded from payment"),
    ]
    for index, (color, label) in enumerate(legend):
        x = 0.027 + index * 0.146
        ax.add_patch(mpatches.FancyBboxPatch(
            (x, 0.038), 0.021, 0.024,
            boxstyle="round,pad=0.002,rounding_size=0.008",
            linewidth=1.0, edgecolor=EDGE, facecolor=color))
        note(ax, x + 0.027, 0.050, label, fontsize=8, ha="left")

    note(ax, 0.5, 0.008,
         "Fail-safe rule: a payment reaches the bank file only when the invoice "
         "amount is proven equal to the requested amount.",
         color="#b02a37", fontsize=9)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
