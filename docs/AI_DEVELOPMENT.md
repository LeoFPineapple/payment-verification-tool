# How this solution was built with AI assistance

The task brief asks candidates to explain *"how you used AI system for autonomous learning
and development"*. This document is that explanation, written from the actual development
record — including the parts where the AI was wrong.

---

## 1. Approach in one line

I used an AI coding agent as the **implementer**, but kept the **decisions** myself: a
written specification up front, one design question asked before any code, tests as the
verification gate, and every suspicious result traced to evidence before it was accepted.

---

## 2. The workflow

### Step 1 — Read the requirement from the artefact, not from a summary

The AI extracted the text of `Task Guideline.pdf` programmatically (PyMuPDF) rather than
being told what it said, then inspected the supplied inputs directly:

- `Payment Request Summary_Sample.xlsx` → 3 rows, columns identified
- `P0001.pdf`, `P0002.pdf`, `P0003.pdf` → rendered to images to discover that two are
  **scanned page images** with no usable text layer, and one is digital

That last finding drove a real design decision: OCR was mandatory, not optional — the
guideline's own requirement ("extract the invoice amount ... using OCR technology") matched
what the sample files actually demanded.

### Step 2 — Resolve ambiguity by asking, not by guessing

Before writing any application code the AI stopped and asked about four genuine forks:

| Decision | Options considered | Chosen |
|---|---|---|
| OCR engine | local offline / cloud LLM vision / text-layer only | local offline, with pluggable cloud vision fallback |
| GUI stack | Streamlit / desktop (Tkinter, PySide6) / Flask | Streamlit |
| Bank file format | combined `004-123456789` / separate columns / customer template | combined, matching the guideline's `012-123142425` example |
| Extra deliverables | README / AI write-up / tests + sample output / diagrams | all four |

An agent that silently guesses at the bank file format produces something that looks
finished and is wrong. Asking cost one round-trip.

### Step 3 — Diagnose the environment before fighting it

Environment probing found Python 3.13 with PyMuPDF, openpyxl, pandas and Streamlit present,
but no OCR engine. Installing RapidOCR then failed with `Errno 13 Permission denied` on a
`*.whl.metadata` file. Rather than retrying blindly, the AI inspected the file ACLs and
found the cause: a `Deny` ACE (`Everyone: DeleteSubdirectoriesAndFiles`) on the workspace
root, which blocks `pip`'s cleanup step.

It then wrote `tools/vendor_ocr.py`, a ~180-line standard-library installer (PyPI JSON API
→ wheel download → unzip into `.pylibs/`, never deleting anything). Two bugs in that script
were caught immediately by testing it:

- a wheel-filename regex that rejected valid `py3-none-any` wheels;
- a platform-tag check that selected **win32** wheels on a **win_amd64** interpreter,
  producing `ImportError: numpy C-extensions incompatible with platform 'win32'`.

Both were fixed, OCR was then proven working on the real scans (English *and* traditional
Chinese amounts read correctly), and only then did application work begin.

### Step 4 — Write the core, then let tests attack it

The library was written module by module, with the safety-critical logic isolated and
commented: `money.py` (Decimal parsing/rejection), `verification.py` (pairing + verdicts),
`bank_file.py` (the required output). A 135-test suite was written alongside, including a
**golden end-to-end test** over the supplied sample files and a test that drives the real
Streamlit GUI through `streamlit.testing.v1.AppTest`.

---

## 3. Bugs the process caught (and why that is the point)

A demo built on "it looked right once" fails in the room. Tests and deliberate
verification found these real defects:

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | All three requests reported `NO_INVOICE` | `KeyError`-style logic bug: confidently matched requests were never written into the `chosen` mapping, so every row fell through to "no match" | store the pairing at assignment time |
| 2 | OCR misread table rows: `Bank Account Number` split from `987654321` | `y // 12` bucketing put a label and its value in different buckets | cluster boxes by **vertical overlap**, then order by `x` and join close boxes with a space |
| 3 | `payee_name` became the caption text | after a cue the parser took the rest of the line, which held the *next* column's caption | split cells on the column gap and reject values that are themselves labels |
| 4 | Bank code/account not found in the Chinese invoice | the value sat in the **next cell** of the same row (`银行编号 | 004`), which was never considered | add same-row adjacent cells as candidates, ranked by proximity |
| 5 | `1.23456` parsed as money `1.23` | regex matched a prefix of a longer number | trailing `(?![\d.])` guard |
| 6 | A clearly different payee was paired because the amount happened to match, and was reported as "amount mismatch" | the amount-based rescue applied even when the payee name was readable and simply did not match | gate the rescue on the name being *unreadable* |
| 7 | An invoice with **no** readable amount still got paired | pairing only required an identity signal, not a usable total | documents with no extractable total cannot support a payment; they are reported as `UNREADABLE` instead |
| 8 | Everything passed with the vendored OCR but the end-to-end tests **failed on a clean `pip install`** | `rapidocr-onnxruntime` declares `Requires-Python <3.13`, so a Python 3.13 pip install silently resolves to the older PP-OCRv3 model, which reads `15,000.00` as `15,ooo.o0` — parsed as **15.00** | repair the two known numeric misreads inside numeric tokens before parsing, and verify the suite against **both** model generations |

Bug 8 is the one worth retelling in the interview. Every test passed locally, the demo
worked, and the failure only appeared when the dependency was re-resolved the way an
interviewer's machine would resolve it. The lesson is that "the tests pass" means nothing
if the tests run against a different environment than the reviewer's. It was found by
deliberately deleting the vendored `.pylibs/` directory and re-running the suite — i.e. by
testing the *installation path*, not just the code.

It also changed the code for the better: the parser is now robust to a whole class of
OCR digit confusion rather than being tuned to one model version, and it still refuses to
read numbers that are not on the page (`ABCO123` is not `ABC0123`, `1.23456` is not
`1.23`).

Bug 7 is the most instructive: it was found only because a test asserted the *reason* for a
verdict, not merely that the bank file was empty. The output was "right" for the wrong
reason, which in a payment control is the same as being wrong.

---

## 4. What I deliberately did not delegate

- **The fail-safe rule** (a payment reaches the bank file only when the amount is *proven*
  equal) is a control decision, not a coding decision. Every ambiguity resolves to "hold
  and explain", never "pay and hope".
- **Amount agreement alone never pairs a request to an invoice.** Two suppliers can bill
  the same round figure; treating that as a match would mask a genuinely missing document.
- **Accepted-by-evidence only.** Every image, rendered page and test result in this project
  was inspected rather than assumed — including the ones that contradicted what the code
  was supposed to be doing.

---

## 5. Verifiable evidence trail

Everything claimed here can be reproduced:

```bash
pytest                                  # 135 passed
pavt "Payment Request Summary_Sample.xlsx" P0001.pdf P0002.pdf P0003.pdf \
     --reference SAMPLE-BATCH --output-dir output
python tools/vendor_ocr.py rapidocr-onnxruntime   # the sandbox workaround
python tools/make_diagram.py                      # regenerates the architecture diagram
```

- **OCR proof:** `P0001.pdf` and `P0002.pdf` have no usable text layer; the tool reports
  `OCR_LOCAL` for them and `TEXT_LAYER` for `P0003.pdf`. The audit report records the route
  per row, so the demo can show it live (`--force-ocr` re-routes `P0003.pdf` through OCR as
  well, with an identical verdict).
- **Correctness proof:** the golden test asserts the exact bank file rows
  (`15000`, `5800`, total `20800`) and that `P000003` appears nowhere in the workbook.
- **Regression proof:** each fix in section 3 has a named test; several are marked in the
  test files as regressions with the observed behaviour described.

---

## 6. Honest limitations

- The parser is **intent-based heuristics**, not a trained document model. It handles the
  supplied layouts plus the variants covered in `tests/`, and it records low confidence and
  warnings rather than pretending certainty on unfamiliar layouts. The optional vision
  fallback exists precisely for that gap.
- OCR of a **new, unseen invoice template** may need a new cue in `_FIELD_CUES`
  (`src/pavt/invoice_parser.py`) — a small, localised change.
- The amount tolerance is a business parameter; the default `0.01` is exposed in the GUI
  and on the CLI, not buried in code.
- The Streamlit download button cannot be auto-clicked from inside the app frame, so the
  file is surfaced with a primary button and a confirmation toast instead of a silent
  auto-download.
