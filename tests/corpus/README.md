# Golden corpus

Real documents with expected outputs, so every OCR setting, prompt, and
segmentation change is **scored rather than guessed at**.

## Layout

```
tests/corpus/
  dd214/
    source.pdf        the real document
    expected.txt      ground truth text, typed by hand
  bad-scan/
    source.jpg
    expected.txt
```

Any directory containing `source.*` and `expected.txt` is picked up
automatically. Run the report with:

```bash
make ocr-report
```

## What belongs here

| Fixture class | Why |
|---|---|
| A real multi-document **military bundle** | The primary use case |
| A **DD-214** | The single document the product is measured on |
| A **VA medical record** excerpt | Dense, form-heavy, high-stakes |
| A clean modern **utility bill** | The easy case; must never regress |
| A **bad scan** — skewed, low contrast, speckled | The OCR quality floor |
| A **handwritten annotation** on a printed form | Known-limitation boundary |
| A **multi-column statement** | Layout robustness |
| A **duplicate pair** (same document, two scan qualities) | Near-duplicate detection |
| A **deed or title** | Known-form registry, vital tier |
| A document with **several plausible dates** | Date-priority rules |

## ⚠️ Handling

These are **genuine personal records** — a DD-214, VA medical records, financial
statements. **They are not in this repository and must never be committed.**
This directory is ignored apart from the harness and this file; the documents
live in the maintainer's working copy and nowhere a clone can reach.

That makes the corpus un-shareable, which is a real cost — a contributor cannot
reproduce the R-01 figure, only re-run the report against fixtures of their own.
It is the cost of the repository being public, and it is the right way round:
the alternative prices a stranger's convenience above a veteran's medical file.

Where a fixture can be redacted without losing its test value, redact it —
locally, and for your own benefit, not as a licence to commit it.

## The gate

Corpus word accuracy must be **≥ 90%** (R-01). Below that, the Phase 3
classification design is built on unreliable text and gets re-planned before any
of it is written. A synthetic run does not clear this gate — only real documents
do.

With no fixtures here, which is every CI runner, the gate is **skipped**, and the
integration job lists it on its summary page with the reason. It is never
reported as a pass.

## The synthetic corpus, next door

`tests/synthetic_corpus.py` is the corpus that *can* be committed: invented
documents (a DD-214-style form, a clinic note, a bill, a two-column statement, a
deed, a W-2, a letter of dates, a fax) rendered and damaged at test time. Ground
truth is exact because it is the text that was drawn. CI scores it on every push
against `tests/synthetic_corpus_baseline.json`. It lives **outside** this
directory on purpose, so that nothing under `tests/corpus/` is ever meant to be
committed.
