---
name: platonic-coding
description: Intelligent orchestrator for the Platonic Coding lifecycle. Auto-detects the user's intent and project state, then routes to the right next step—brainstorm a design, initialize a project, run the recovery flow for existing code, formalize drafts into RFCs, refine specs, implement from guides with tests, or review code compliance. Single entry point for the complete specification-driven development lifecycle, with structured design exploration built in.
license: MIT
metadata:
  version: "2.5.0"
  author: "Xiaming Chen"
  category: "workflow"
  replaces:
    - platonic-init
    - platonic-specs
    - platonic-impl
    - platonic-code-review
    - platonic-workflow
    - platonic-brainstorming
---

# Platonic Coding

Intelligent orchestrator for the complete **specification-driven development lifecycle**. Auto-detects the user's intent and project state, then executes the appropriate workflow phase—brainstorming, initialization, specification management, implementation, or review. Structured design exploration (formerly the standalone `platonic-brainstorming` skill) is built in as **BRAINSTORM mode**.

## When to Use This Skill

Use this skill when you need to:

- **Brainstorm** a design through collaborative dialogue before committing to an RFC
- **Bootstrap** a new project with Platonic Coding infrastructure
- **Adopt** Platonic Coding for an existing codebase (recover specs from code)
- **Manage** RFC specifications (validate, refine, generate indices)
- **Implement** features from RFC specs with guides and tests
- **Review** code for spec compliance
- **Run** the full workflow from design → RFC → code → review

**Keywords**: platonic coding, brainstorm, discuss, investigate, specs, RFC, implementation, review, workflow, spec-driven, initialization

## Intelligent Auto-Detection

Auto-detects intent and project state when invoked without specific instructions. See `references/REFERENCE.md` for the full decision tree.

### Intention Detection (runs first)

Before inspecting project state, scan the user's natural-language request for **intent keywords** that signal a desire to explore rather than immediately produce an artifact:

- **Trigger words** (case-insensitive): `brainstorm`, `discuss`, `investigate`, `explore`, `deep analysis`, `deep dive`, `think through`, `design exploration`, `trade-offs`, `what's the best approach`.
- **Match** → enter **BRAINSTORM mode** (`references/BRAINSTORM/brainstorm.md`), regardless of project state. The user wants to explore first; project-state detection resumes at handoff.
- **Exception**: if the request uses a canonical operation name (`impl-full`, `review`, `specs-refine`, `init-scaffold`, `workflow --phase <N>`, `brainstorm`), the explicit operation wins — don't reroute it.

### Project-State Detection (when no intent keywords)

**Quick Reference**:
- No Platonic infrastructure (no `.platonic.yml` **and** no `docs/specs/`) → INIT mode (scaffold new or recover existing)
- RFCs but no guides → Phase 2 (implementation)
- Guides + code → Phase 3 (review) or resume Phase 2

**Note**: `.platonic.yml` is optional. The skill auto-detects project metadata (name, language, framework) from host manifests and uses built-in default paths (`docs/specs`, `docs/impl`, `docs/drafts`). A `.platonic.yml` is only needed to override defaults or set custom spec stages.

**Override** with canonical operations: `brainstorm`, `init-scaffold`, `init-scan`, `specs-refine`, `impl-full`, `review`, `workflow --phase <N>`.

## Core Workflow Phases

| Phase | Focus | Output | Brainstorming |
|-------|-------|--------|---------------|
| **1** | RFC Specification | Draft → RFC → `specs-refine` | ✅ Optional (BRAINSTORM mode) |
| **2** | Implementation | Guide → Code + Tests | ✅ Optional (BRAINSTORM mode) |
| **3** | Review | Compliance report | ❌ |

Phases run sequentially (full workflow) or independently (explicit invocation). BRAINSTORM mode is an optional entry ramp to Phases 1 and 2. See `references/WORKFLOW/workflow-overview.md` for details.

## Operation Modes

### BRAINSTORM Mode
Structured design exploration through collaborative dialogue. Produces an approved design draft (`docs/drafts/`), then presents **post-draft alternative paths** (see below). Entered by intent keywords, or explicitly. No code, RFC, or scaffolding is produced until the design is approved.

**Examples**: `brainstorm the message queue design`, `discuss how auth should work`, `investigate options for caching`.

### INIT Mode
Bootstrap Platonic Coding infrastructure. Operations: `init-scaffold`, `init-scan`, `init-plan-modular-specs`, `init-recover-*`.

**Note**: `init-scaffold` creates directories and templates but does **not** create `.platonic.yml` by default — project metadata is auto-detected from host manifests and default paths are used. A `.platonic.yml` is only created when the user explicitly requests non-default paths or custom spec stages.

**Examples**: `init-scaffold for project "Acme"`, `init-scan then recover specs`.

### SPECS Mode
Manage RFC specifications. Operations: `specs-refine` (comprehensive), `specs-generate-*`, `specs-validate-*`, `specs-check-*`.

**Examples**: `specs-refine`, `specs-generate-index`.

### IMPL Mode
Translate RFCs to guides and code. Operations: `impl-full` (default), `impl-create-guide`, `impl-code`, `impl-validate-guide`, `impl-update-guide`.

**Confirmation Gates**: Pauses after impl guide and coding plan. Override with "no confirmations".

**Examples**: `impl-full for RFC-042`, `impl-code from IG-001`.

### REVIEW Mode
Validate code against specs. Generates report-only by default (no code changes). 6-step process: understand specs → checklist → map → review → discrepancies → report.

**Examples**: `review src/auth/ against RFC-001`, `review for gap analysis`.

### WORKFLOW Mode
Orchestrate full 3-phase flow. See `references/WORKFLOW/workflow-overview.md`.

**Examples**: `workflow --phase 1`, `workflow starting with brainstorm`.

---

For detailed guides, see `references/REFERENCE.md`.

## Post-Draft Alternative Paths

After BRAINSTORM mode produces an approved design draft, the skill does **not** assume "create a new RFC" is the only next step. It checks `docs/specs/` and `docs/impl/` for related existing RFCs/IGs, then presents a routing menu (full table in `references/BRAINSTORM/brainstorm.md`):

1. **Pause at each phase gate** — careful, default; approve every confirmation gate
2. **Quick pass** — run multiple phases straight through, no further confirmations (quick fixes)
3. **Update an existing RFC** — revise a related RFC instead of spawning a new one
4. **Create a new RFC** — formalize the draft into a fresh `RFC-NNN` (next sequential `NNN` unless user overrides)
5. **Update an existing IG** — refine an existing implementation guide via `impl-update-guide`
6. **Create a new IG** — generate a fresh `IG-XXX` via `impl-create-guide` (next sequential `XXX` unless user overrides)

The menu leads with a recommendation based on what was found. The user picks by number; each path maps to existing canonical operations.

## Default Paths & Templates

| Artifact | Path | Naming |
|----------|------|--------|
| Drafts | `docs/drafts/` | `YYYY-MM-DD-<topic>-design.md` |
| RFCs | `docs/specs/` | `RFC-NNN-<name>.md` (`NNN` = exactly 3 digits; strict sequence) |
| Guides | `docs/impl/` | `IG-XXX-<name>.md` (`XXX` = exactly 3 digits; strict sequence) |

Templates in `assets/` use `{{PLACEHOLDER}}` syntax.

## Best Practices

1. Trust auto-detection; override with explicit operations when needed
2. Review generated artifacts (all Draft status)
3. Run `specs-refine` regularly
4. Use confirmation gates—don't skip unless confident (or use quick-pass routing)
5. Review mode is report-only by default

## Dependencies

- Read/write to project directories
- Access to host manifests (`Cargo.toml`, `package.json`, `pyproject.toml`, `go.mod`, etc.) for auto-detection
- `.platonic.yml` is **optional** — only read if present for path/stage overrides
- Understanding of target language/framework
