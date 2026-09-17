# Backends (external skill contracts)

All three backends are external skills, referenced by name. The bootstrap holds their *contracts*,
not their content. Register them in `nano.yml`:

```yaml
skills:
  - /path/to/oh-my-research/skills/oh-my-research
  - /path/to/academic-research-skills/deep-research
  - /path/to/autoresearch          # uditgoenka/autoresearch on GitHub
```

| Backend | Source | License | Owns |
|---|---|---|---|
| `omr` | `caesar0301/oh-my-research` | Apache-2.0 | collect, analyze, think, synth |
| `ars` | `Imbad0202/academic-research-skills` | CC BY-NC 4.0 | verify, review, question, paper |
| `auto` | `uditgoenka/autoresearch` | (external) | iterate: modify → verify → keep/discard |

## omr — oh-my-research (report-first)

**Strengths:** disk-as-source-of-truth, incremental long-report writing, evidence grades
(`suggests`/`proven`/`inferred`), publication-safety boundary, DOCX/PDF export.

**Nodes it owns:**

| Node | Op | Output |
|---|---|---|
| collect | `collect <url\|doi\|query>` | `materials/**`, `docs/index/papers-index.json` |
| analyze | `analyze` | `docs/plans/{brief,evidence,judgment}-*.md` |
| think | `think [method]` | refined artifact (THINK playbook, outcome stamp) |
| synth | `synth --mode … --format docx\|pdf` | `docs/<mode>/` + rendered deliverable |

**Entry contract:** OMR `docs/index/papers-index.json` uses stable IDs `P-001`, `W-001`, `G-001`,
`S-001`. Each paper entry: `{id, source, title, collected_at, path}`.

## ars — academic-research-skills (rigor)

**Strengths:** 13-agent team, adversarial checkpoints (devil's advocate, editor-in-chief, ethics),
citation verification, PRISMA/RoB/APA methodology knowledge.

**Nodes it owns:**

| Node | Agent(s) | Output |
|---|---|---|
| question | research_question_agent / socratic_mentor | RQ brief (FINER-scored) |
| verify | source_verification_agent, bibliography_agent | graded sources, evidence hierarchy |
| review | editor_in_chief_agent, ethics_review_agent, devils_advocate_agent | editorial verdict, integrity verdict |
| paper | academic-paper (12 agents) | manuscript |

**Entry contract:** Material Passport `literature_corpus[]` — each entry requires `citation_key`,
`title`, `authors` (CSL-JSON), `year`, `source_pointer`. See
`academic-research-skills/shared/contracts/passport/literature_corpus_entry.schema.json`.

## auto — autoresearch (iteration loop)

**Strengths:** bounded autonomous iteration, modify → verify → keep/discard against a predicate,
orchestrator goal-archetype routing, adversarial debate (`reason`), persona debates (`predict`,
`probe`).

**Nodes it owns:**

| Node | Subcommand | Output |
|---|---|---|
| iterate | bare `$autoresearch` / `$autoresearch reason` | handoff.json + converged artifact |

**Entry contract:** `autoresearch/` output dirs + `handoff.json` (version, source, status,
findings, config). The bootstrap reads `handoff.json` to fold loop results back into the graph.

## Routing rule

A node routes to exactly one backend. The `routes` map in each pattern file is the single source
of truth. Cross-backend artifacts move only through the two adapters in `scripts/`.
