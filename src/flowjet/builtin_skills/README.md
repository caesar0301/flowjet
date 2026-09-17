# fj Builtin Skills

Skills shipped with the **fj** CLI. Registered automatically via
``register_builtin_skill_root`` when the agent starts.

Includes workflow skills (planning, TDD, debugging, code review, git worktrees)
and document skills (`docx`, `pptx`, `xlsx`, `pdf`, `mcp-builder`).

Add a new skill as ``<name>/SKILL.md`` under this directory (AgentSkills format).
Skill scripts under this tree are excluded from package ruff lint.
