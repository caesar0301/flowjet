# Phase 2: Implementation

**[Phase 2] Implementation**

## Objective

Produce implementation guide from RFC spec, then implement code with tests. Optional: BRAINSTORM mode for design refinement.

## Process

1. **Optional brainstorming**: Enter BRAINSTORM mode (see `references/BRAINSTORM/brainstorm.md`) for architecture validation before coding. On approval, the post-draft routing menu may route into `impl-update-guide` (existing IG) or `impl-create-guide` (new IG).
2. **Identify RFC**: Ask user if not specified; determine target module/language/framework
3. **Run impl-full**: Read `references/IMPL/impl-full.md`
   - Spec Analysis: Extract requirements, constraints, invariants
   - Impl Guide Design: Create guide (**confirmation gate**)
   - Coding Plan: Task breakdown (**confirmation gate**)
   - Coding: Implement + unit/integration tests
4. **Verify**: Guide exists, code written, tests passing

## Inputs

- RFC spec (from Phase 1)
- RFC number/index (ask if not specified)
- Target module/language/framework

## Output

- Implementation guide (`IG-XXX-<name>.md`; `XXX` = exactly 3 digits, next sequential unless user overrides)
- Source code
- Unit + integration tests

## Handoff to Phase 3

Confirm code paths and RFCs/guides to review. Proceed to Phase 3 (REVIEW mode).