# Routing — intent → pattern

## Decision order

1. Canonical pattern name (`--pattern <name>`) — wins over everything.
2. Intent signals below, strongest match first.
3. Workspace state (resume an active `.bootstrap/state.json`).
4. Ambiguous → show the pattern library and ask.

## Intent signals

| Signal (case-insensitive) | Pattern |
|---|---|
| `systematic review`, `PRISMA`, `meta-analysis`, `meta分析`, `系统综述` | `systematic-review` |
| `write a paper`, `manuscript`, `submit`, `publish`, `写论文`, `投稿` | `paper-pipeline` |
| `fact-check`, `verify these claims`, `事实核查`, `查证` | `evidence-deep` (verify-only tail) |
| `quick`, `brief`, `summary only`, `快速`, `简报` | `rapid` |
| `brainstorm`, `what if`, `speculate`, `idea`, `灵感`, `头脑风暴` | `idea-first` |
| `position`, `stance`, `take a side`, `argue that`, `立场`, `论证` | `stance-first` |
| `keep improving`, `iterate until`, `converge`, `迭代`, `收敛` | `verify-loop` |
| `monitor`, `track this topic`, `updates`, `监测`, `追踪` | `loop` |
| (no strong signal; has a clear research question) | `evidence-deep` |
| (no strong signal; vague topic, no RQ) | `idea-first` |

## Signal precedence

`synthetic-verb signals` (write/submit/verify/iterate) outrank `topic signals` (systematic review).
A single request can carry both "systematic review" and "write a paper" — the *deliverable*
verb wins: `paper-pipeline` if the goal is a manuscript, `systematic-review` if the goal is the
review itself.

## Workspace-state detection (when no strong intent)

1. `.bootstrap/state.json` exists with `active: true` → resume the active pattern.
2. OMR materials/papers-index present, no judgment → `evidence-deep` (analyze + verify).
3. OMR judgment present, no synth → `evidence-deep` (synth + review tail).
4. ARS passport `literature_corpus[]` present, no OMR workspace → `rigor-first`.
5. New evidence contradicts published claims → propose `verify-loop`.

## Recommended build/use order

1. `evidence-deep` — flagship; OMR graph + two ARS nodes injected.
2. `verify-loop` — the autoresearch integration; makes the bridge convergent.
3. `rigor-first` — needs `papers_index_to_csljson.py` (the omr→ars adapter).
4. `paper-pipeline` — rides ARS's existing deep-research → academic-paper handoff.
5. `systematic-review` — niche but deep.
6. `rapid`, `evidence-first` — trivial variants.
7. `idea-first`, `stance-first` — socratic/decide variants.
8. `loop` — monitoring cadence, least one-shot.

Items 1–3 unblock the whole library; 4–10 are new `routes` maps on already-built plumbing.
