---
name: research-bootstrap
description: "Orchestrator that routes research work across three external skills — oh-my-research (report-first analysis), academic-research-skills (rigor, verification, paper writing), and autoresearch (iteration/verification loop) — using JSON workflow patterns with per-node backend routing. Triggers on: deep research, literature review, survey, report, research-to-paper, systematic review, bootstrap research, bridge research, 深度研究, 文献综述, 调研报告."
version: 0.1.0
---

# Research Bootstrap

A **pattern-driven orchestrator** that bridges three external research skills. It does not
reimplement any of them — it routes each workflow node to whichever backend does that node best,
and adapts artifacts across the boundaries.

**North star:** route each phase of research to the backend that is strongest at it, then pass
clean artifacts across the seams.

## Backends (all external — referenced, never vendored)

| Backend | External skill | Source | Role in bootstrap |
|---|---|---|---|
| `omr` | oh-my-research | `caesar0301/oh-my-research` | collect → analyze → think → synth (report-first, disk-state, incremental) |
| `ars` | academic-research-skills | `Imbad0202/academic-research-skills` | verify → review → question → paper (adversarial, citation, methodology rigor) |
| `auto` | autoresearch | `uditgoenka/autoresearch` | modify → verify → keep/discard iteration loop |

All three are registered as external skills via `nano.yml` `skills:` and invoked by name.
The bootstrap skill itself ships **only routing + patterns + adapters** — no backend content.

> **License note:** `ars` (academic-research-skills) is CC BY-NC 4.0. The bootstrap references it
> by name only and never copies its content. Keep it that way.

## Dispatch

Parse the invocation in this order:

1. **Canonical pattern name** — `bootstrap --pattern <name>` wins over everything.
2. **Intent signals** — match against `references/routing.md`, select a pattern.
3. **Workspace state** — if a pattern is already active in `.bootstrap/state.json`, resume it.
4. **Ambiguous** — show the pattern library and ask.

Print a banner on every invocation: `[bootstrap] pattern: <name> | backends: omr, ars, auto`.

## Patterns

| Pattern | Route map | Gates | Use when |
|---|---|---|---|
| `evidence-deep` | collect/analyze/think/synth=**omr**; verify/review=**ars** | M→A→T→P→D | Default. Survey/report with adversarial checkpoints |
| `evidence-first` | collect/analyze/synth=**omr**; review=**ars** | M→A→P→D | Lighter; one review pass |
| `rapid` | all=**omr** | off | Quick brief, no rigor |
| `rigor-first` | research/source-verify/synthesis=**ars**; collect/synth=**omr** | ARS checkpoints + D | ARS deep-research → OMR long report |
| `paper-pipeline` | research+paper+review=**ars**; ingest/export=**omr** | ARS mandatory | Research → manuscript → peer review |
| `systematic-review` | PRISMA/RoB/meta=**ars**; report synth=**omr** | ARS compliance | PRISMA systematic review + meta-analysis |
| `idea-first` | idea=**omr**; question=**ars** (socratic); analyze/synth=**omr** | M→A→P→D | Vague idea, guided RQ formulation |
| `stance-first` | decide=**omr**; analyze/synth=**omr**; adversarial=**ars** | B→A→P→D | Position/claim paper with devil's-advocate stress test |
| `verify-loop` | produce=**omr**; verify=**auto**+**ars**; revise=**omr** | L (iterate vs advance) | Converge claims before shipping |
| `loop` | collect/analyze=**omr**; gap-detect=**ars** | L cycles | Ongoing monitoring + drift reconciliation |

Pattern files: `patterns/*.json`. See `references/routing.md` for intent → pattern selection.

## Node → Backend Contract

| Node | Backend | Produces |
|---|---|---|
| `collect` | omr | `materials/**`, `docs/index/papers-index.json` |
| `analyze` | omr | `docs/plans/{brief,evidence,judgment}-*.md` |
| `think` | omr | refined artifact (THINK playbook) |
| `verify` | ars | verification/graded sources (source_verification_agent) |
| `review` | ars | editorial + ethics + devil's-advocate verdict |
| `question` | ars | RQ brief (research_question_agent / socratic_mentor) |
| `synth` | omr | `docs/<mode>/` long report + DOCX/PDF export |
| `paper` | ars | manuscript (academic-paper) |
| `iterate` | auto | modify → verify → keep/discard loop |

## Artifact Adapters (the seams)

Two deterministic adapters translate between OMR and ARS formats:

| Adapter | Direction | Transform |
|---|---|---|
| `scripts/papers_index_to_csljson.py` | omr → ars | `docs/index/papers-index.json` → Material Passport `literature_corpus[]` (CSL-JSON) |
| `scripts/csljson_to_omr.py` | ars → omr | `literature_corpus[]` → `docs/index/papers-index.json` + `_citation-map.md` |

Adapters are thin and deterministic (no network, no LLM). Metadata enrichment (authors, year,
venue) is expected to come from the OMR `analyze` step or the `_citation-map.md` — never invented
by the adapter.

## State

`.bootstrap/state.json` — bootstrap-owned, additive. Tracks: active pattern, completed nodes,
current backend, last adapter run, per-node outcomes. Each backend keeps its own state (`.omr/`
for OMR, Material Passport for ARS, `autoresearch/` output dirs for auto). The bootstrap reads
them; it does not own them.

## Safety Invariants

1. **Never relicense ARS content** — reference by name, never copy into this repo.
2. **No silent backend substitution** — a node routed to `ars` stays on `ars`; switching backends
   is a pattern change, announced to the user.
3. **Adapters never invent metadata** — a missing `year`/`authors` goes to `rejection_log.yaml`,
   not a placeholder (mirrors ARS's own adapter discipline).
4. **No un-gated ship** — `paper` and `synth` publish nodes require their backend's own gates
   (ARS mandatory checkpoints, OMR Gate D) before any deliverable leaves the workspace.

## References

| File | Purpose |
|---|---|
| `references/routing.md` | Intent detection + pattern selection table |
| `references/backends.md` | External skill contracts (what each owns) |
| `references/handoff-omr-ars.md` | OMR artifacts → ARS ports |
| `references/handoff-ars-omr.md` | ARS artifacts → OMR ports |
