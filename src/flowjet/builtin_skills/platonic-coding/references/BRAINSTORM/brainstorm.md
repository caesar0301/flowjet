# Brainstorm Mode

## Objective

Turn an idea into a fully formed, validated **design draft** through collaborative dialogue, then route to the right downstream Platonic Coding phase. Brainstorming produces no code, no RFC, and no scaffolding — only an approved design draft and a routing decision.

This mode is the in-skill successor to the standalone `platonic-brainstorming` skill. It is used optionally at Phase 1 (before RFC formalization) and Phase 2 (before implementation), and is entered directly when the user's intent signals exploration (see Intention Detection in `SKILL.md`).

<HARD-GATE>
Within brainstorm mode, do NOT write any code, scaffold any project, generate any RFC, or take any implementation action until you have presented a design and the user has explicitly approved it. The only output of this mode is a design draft plus a downstream routing choice.
</HARD-GATE>

## Anti-Pattern: "This Is Too Simple To Need A Design"

For work that enters Platonic Coding phases, even "simple" changes deserve an explicit design pass so assumptions are surfaced early. The design can be short (a few sentences for truly simple projects), but it should still be presented and approved before the workflow advances.

## Checklist

Create tasks for the applicable steps below and follow them in process order:

1. **Explore project context** — check files, docs, recent commits
2. **Ask clarifying questions** — one at a time, understand purpose / constraints / success criteria
3. **Propose 2–3 approaches** — with trade-offs and your recommendation
4. **Present design** — validate sections incrementally as needed, then get explicit approval on the overall design before drafting
5. **Write design draft** — save to `docs/drafts/YYYY-MM-DD-<topic>-design.md` by default, or update the user-provided draft if one already exists
6. **Draft self-review** — quick inline check for placeholders, contradictions, ambiguity, scope (see below)
7. **User reviews written draft** — ask user to review the draft file before proceeding
8. **Post-draft routing** — present the alternative paths below and route to the chosen one

## Process Flow

```
Explore project context
        │
        ▼
Ask clarifying questions (one at a time)
        │
        ▼
Propose 2–3 approaches ─── (user picks) ──┐
        │                                 │
        ▼                                 │
Present design sections ◄─────────────────┘ (revise)
        │
        ▼
   Overall design approved? ── no ──► Present design sections (revise)
        │ yes
        ▼
Write design draft
        │
        ▼
Draft self-review (fix inline)
        │
        ▼
   User reviews draft? ── changes ──► Write design draft (revise)
        │ approved
        ▼
Post-draft routing menu (see below)
        │
        ▼
Hand off to Phase 1 (RFC) or Phase 2 (impl)
```

## The Process

**Understanding the idea:**

- Check out the current project state first (files, docs, recent commits)
- Before asking detailed questions, assess scope: if the request describes multiple independent subsystems (e.g., "build a platform with chat, file storage, billing, and analytics"), flag this immediately. Don't spend questions refining details of a project that needs to be decomposed first.
- If the project is too large for a single design draft, help the user decompose into sub-projects: what are the independent pieces, how do they relate, what order should they be built? Then brainstorm the first sub-project through the normal design flow. Each sub-project gets its own draft → downstream Platonic stages → implementation cycle.
- For appropriately-scoped projects, ask questions one at a time to refine the idea
- Prefer multiple choice questions when possible, but open-ended is fine too
- Only one clarifying or design question per message — if a topic needs more exploration, break it into multiple questions
- Focus on understanding: purpose, constraints, success criteria

**Exploring approaches:**

- Propose 2–3 different approaches with trade-offs
- Present options conversationally with your recommendation and reasoning
- Lead with your recommended option and explain why

**Presenting the design:**

- Once you believe you understand what you're building, present the design
- Scale each section to its complexity: a few sentences if straightforward, up to 200–300 words if nuanced
- Validate sections incrementally when that helps the user stay aligned, but do not treat those checkpoints as final approval
- Get one explicit approval on the overall design before you write the draft
- Cover: architecture, components, data flow, error handling, testing
- Be ready to go back and clarify if something doesn't make sense

**Design for isolation and clarity:**

- Break the system into smaller units that each have one clear purpose, communicate through well-defined interfaces, and can be understood and tested independently
- For each unit, you should be able to answer: what does it do, how do you use it, and what does it depend on?
- Can someone understand what a unit does without reading its internals? Can you change the internals without breaking consumers? If not, the boundaries need work.
- Smaller, well-bounded units are also easier for you to work with — you reason better about code you can hold in context at once, and your edits are more reliable when files are focused. When a file grows large, that's often a signal that it's doing too much.

**Working in existing codebases:**

- Explore the current structure before proposing changes. Follow existing patterns.
- Where existing code has problems that affect the work (e.g., a file that's grown too large, unclear boundaries, tangled responsibilities), include targeted improvements as part of the design, the way a good developer improves code they're working in.
- Don't propose unrelated refactoring. Stay focused on what serves the current goal.

## After the Design

**Documentation:**

- Write the validated design draft to `docs/drafts/YYYY-MM-DD-<topic>-design.md`
  - (User preferences or an existing draft path override this default)
- Use the `elements-of-style:writing-clearly-and-concisely` skill if available
- Commit the design draft to git only if the user wants the draft versioned now

**Draft Self-Review:**
After writing the design draft, look at it with fresh eyes:

1. **Placeholder scan:** Any "TBD", "TODO", incomplete sections, or vague requirements? Fix them.
2. **Internal consistency:** Do any sections contradict each other? Does the architecture match the feature descriptions?
3. **Scope check:** Is this focused enough for a single implementation plan, or does it need decomposition?
4. **Ambiguity check:** Could any requirement be interpreted two different ways? If so, pick one and make it explicit.

Fix any issues inline. This self-review is the internal cleanup pass before the user review gate. If the user later requests changes, make them and run this quick self-review again before re-presenting the draft.

For a deeper, independent check, you may dispatch the design-draft reviewer subagent using the template at `assets/brainstorming/spec-document-reviewer-prompt.md`. This is optional — the inline self-review above is the baseline.

**User Review Gate:**
After the draft review loop passes, ask the user to review the written draft before proceeding:

> "Design draft written to `<path>`. Please review it and let me know if you want to make any changes before we route to the next step."

Wait for the user's response. If they request changes, make them and re-run the draft review loop. Only proceed to routing once the user approves.

## Post-Draft Routing (alternative paths)

Once the draft is approved, do **not** assume the only next step is "create a new RFC." First check `docs/specs/` and `docs/impl/` for existing RFCs or IGs related to this draft's topic (match by semantic name / topic keywords), then present a routing menu so the user chooses how to continue.

Present the menu as a numbered list with a one-line description per option, and lead with a recommendation based on what you found (e.g., "no existing RFC for this topic → recommend #4"). The user picks a number or describes a combination.

| # | Path | When to use | Routes to |
|---|------|-------------|-----------|
| 1 | **Pause at each phase gate** (default, careful) | The change is non-trivial and the user wants to approve each step | Phase 1 RFC formalization, pausing at every confirmation gate (RFC number, impl guide, coding plan) |
| 2 | **Quick pass** (multiple phases, no further confirm) | Small scope, quick fix, user is confident — wants draft → RFC → specs-refine → impl-full → (optional review) in one chain | Phase 1 → Phase 2 (auto-mode, gates skipped) → optional Phase 3, one approval covers the chain |
| 3 | **Update an existing RFC** | A related RFC already exists; this draft should revise it rather than spawn a new one | Formalize draft as a revision to `RFC-NNN` (bump version / change history), then `specs-refine` |
| 4 | **Create a new RFC** | No related RFC exists, or the draft is a distinct new concern | Formalize draft into a fresh `RFC-NNN-<name>.md` (next sequential `NNN` unless user overrides), then `specs-refine` |
| 5 | **Update an existing IG** | An implementation guide already exists for the target; the draft refines its design | `impl-update-guide` against the existing `IG-XXX` (draft informs the IG revision) |
| 6 | **Create a new IG** | A new RFC exists (or will) and needs a fresh implementation guide | `impl-create-guide` producing a fresh `IG-XXX-<name>.md` (next sequential `XXX` unless user overrides) |

**Routing rules:**

- Paths 3 and 5 require a related existing RFC/IG — only offer them if you actually found one. State which one (e.g., "update RFC-007-message-queue").
- Path 2 (quick pass) still respects the IMPL mode's own guardrails: if scope turns out larger than expected mid-flight, pause and surface it rather than barreling through. "Quick pass" is a consent to skip *confirmation* gates, not a license to skip *validation*.
- After the chosen path completes its first hop (RFC created/updated, or IG created/updated), the normal phase handoff resumes — the user is not locked into the whole chain unless they picked path 2.
- Whatever the choice, the terminal handoff lands inside Platonic Coding's existing phases (1 or 2), never an external implementation skill.

## Key Principles

- **One question at a time** — don't overwhelm with multiple questions
- **Multiple choice preferred** — easier to answer than open-ended when possible
- **YAGNI ruthlessly** — remove unnecessary features from all designs
- **Explore alternatives** — always propose 2–3 approaches before settling
- **Incremental validation** — use section-by-section checkpoints to keep alignment, then get explicit approval on the overall design before drafting
- **Be flexible** — go back and clarify when something doesn't make sense
