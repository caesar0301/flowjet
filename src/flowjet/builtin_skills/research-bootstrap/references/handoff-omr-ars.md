# Handoff: OMR → ARS

Run `scripts/papers_index_to_csljson.py` to translate OMR's paper index into an ARS Material
Passport `literature_corpus[]`. Then hand the mapped artifacts to ARS's `deep-research` /
`academic-paper` intake.

## Artifact mapping

| OMR artifact | ARS port | What ARS skips |
|---|---|---|
| `docs/index/papers-index.json` | `literature_corpus[]` (CSL-JSON) | bibliography search |
| `docs/plans/brief-{id}.md` | research_question_agent input | Phase 1 scoping |
| `docs/plans/judgment-{id}.md` | synthesis_agent context | re-deriving findings |
| `docs/plans/evidence-{id}.md` | source_verification_agent context | evidence hierarchy pass |
| `docs/<mode>/_citation-map.md` | bibliography enrichment (authors/venue) | citation metadata |

## Field mapping (papers-index → literature_corpus)

| OMR field | ARS field | Rule |
|---|---|---|
| `id` (`P-001`) | `citation_key` | sanitize to `^[A-Za-z][A-Za-z0-9_:-]*$`; keep `P-001` if valid, else slugify title |
| `source` (arxiv URL) | `arxiv_id` + `source_pointer` | regex-extract arxiv id; keep full URL as pointer |
| `source` (DOI) | `doi` + `source_pointer` | strip `doi:`/URL prefix; keep full URL as pointer |
| `title` | `title` | as-is |
| arxiv id (`2401.xxxxx`) | `year` | derive `20` + first two digits (deterministic, no network) |
| `collected_at` | `obtained_at` | as-is |
| `path` | `source_pointer` (file fallback) | when no URL |

## Evidence-grade mapping

OMR's evidence grades map onto ARS's claim-strength ladder:

| OMR grade | ARS treatment |
|---|---|
| `proven` | claim is fully supported; keep as-is |
| `suggests` | weight evidence but do not upgrade |
| `inferred` | mark speculative; exclude as an anchor |

**Never upgrade `suggests` → `proven` across the boundary.** The adapter preserves grades; it does
not reinterpret them.

## Rejections

Entries missing a derivable `year` **and** without authors/venue in the citation map are written
to `rejection_log.yaml` (mirrors ARS's adapter discipline) — never coerced to placeholders.

## Then

After the adapter runs, invoke ARS `deep-research` (or `academic-paper`). ARS's `intake_agent`
auto-detects the available materials and skips redundant phases (RQ brief → skip scoping,
bibliography → skip search, synthesis → accelerate findings/discussion).
