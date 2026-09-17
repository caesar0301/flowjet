# Handoff: ARS → OMR

Run `scripts/csljson_to_omr.py` to translate ARS's Material Passport `literature_corpus[]` back
into OMR's paper index + citation map. Use when ARS did the rigorous research and OMR writes the
long, publication-quality report.

## Artifact mapping

| ARS artifact | OMR destination |
|---|---|
| `literature_corpus[]` (CSL-JSON) | `docs/index/papers-index.json` (papers bucket) |
| bibliography (APA 7.0) | `docs/<mode>/_citation-map.md` |
| RQ brief (FINER-scored) | `docs/plans/brief-{id}.md` research question |
| synthesis report | `docs/plans/judgment-{id}.md` (fast analyze pass) |
| methodology blueprint | `docs/plans/plan-{id}.md` |

## Field mapping (literature_corpus → papers-index)

| ARS field | OMR field | Rule |
|---|---|---|
| `citation_key` | `id` | remap to `P-{n:03d}` in index order; keep `citation_key` in `_citation-map.md` |
| `title` | `title` | as-is |
| `source_pointer` | `source` | as-is (URL/DOI/arxiv/file) |
| `year` + `authors` | (citation map) | full reference entry: `Author, A. (Year). Title. Venue. DOI` |
| `doi` / `arxiv_id` | (citation map) | append to full reference |

## Why this direction matters

ARS's `report_compiler` produces prose but has no incremental, resumable long-form pipeline with
styled DOCX/PDF export. OMR's `synth` (outline → chapter-per-turn → continuity → `export_report.py`)
is exactly that. Feed ARS's synthesis into OMR `analyze` (fast pass — the judgment *is* the
synthesis), then `synth --mode survey --format docx`.

## Citation-key ↔ material-ID lockstep

The one mapping that must stay identical in both directions: `citation_key` (ARS) ↔ `P-{id}`
(OMR). Keep it in `_citation-map.md` so OMR's internal traceability and ARS's bibliography never
drift.

## Then

After the adapter runs: OMR `collect` is skipped (materials already indexed), `analyze` is a fast
confirm of ARS's synthesis, `synth` writes the report. Gate A / Gate D still apply — no gate is
bypassed just because ARS did the rigor.
