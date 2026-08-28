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
statements. They live in this private repo and nowhere else. Where a fixture can
be redacted without losing its test value, redact it. **Never make this
repository public.**

## The gate

Corpus word accuracy must be **≥ 90%** (R-01). Below that, the Phase 3
classification design is built on unreliable text and gets re-planned before any
of it is written. A synthetic run does not clear this gate — only real documents
do.
