# Phase 1: RFC Specification

**[Phase 1] RFC Specification**

## Objective

Produce RFC (Status: Draft) and refine via `specs-refine`. Optional: use BRAINSTORM mode for design exploration.

## Process

1. **Optional brainstorming**: Enter BRAINSTORM mode (see `references/BRAINSTORM/brainstorm.md`) for conceptual design exploration. On approval, the post-draft routing menu may route directly into step 3 or 4.
2. **Create draft**: Use existing draft or create interactively (problem, constraints, abstractions, terminology). Save to `docs/drafts/YYYY-MM-DD-<topic>-design.md`
3. **Determine RFC index**: Suggest the next sequential `NNN` (exactly three numeric characters, zero-padded). Use a non-sequential number only if the user explicitly requests it.
4. **Generate RFC**: Formal RFC following `templates/rfc-standard.md`. Write to `docs/specs/RFC-NNN-<name>.md`
5. **Refine**: Run `specs-refine` (updates history, index, namings; validates consistency)

## Output

- RFC in `docs/specs/` (Status: Draft)
- Updated supporting files
- Optional: Design draft in `docs/drafts/`

## Handoff to Phase 2

Confirm RFC path/identifier. Proceed to implementation. Optional: enter BRAINSTORM mode for design refinement.