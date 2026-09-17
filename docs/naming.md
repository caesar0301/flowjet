# FlowJet naming

**Status:** Approved
**Date:** 2026-07-26
**Updated:** 2026-09-17 (RFC-004: distribution renamed to `flowjet`; `fj_ai` retired)

## Identity

| Object | Value | Role |
|--------|-------|------|
| Product brand | **FlowJet** | Human-facing product name |
| PyPI / `[project].name` | **`flowjet`** | Installable distribution (CLI **and** server) |
| Source repository | **`flowjet-agent`** | GitHub repo (`caesar0301/flowjet-agent`) |
| Formal CLI | **`flowjet`** | Canonical console entrypoint |
| CLI alias | **`fj`** | Same as `flowjet` |
| CLI alias | **`fjf`** | Same as `flowjet -f` / `fj -f` |
| Server entrypoint | **`flowjet-server`** | Canonical console script (Docker / compose) |
| Server alias | **`fj serve`** | Same service from the CLI dispatcher |
| Python import package | **`flowjet`** | `flowjet.core`, `flowjet.cli`, `flowjet.server` |

## Rules

1. **Brand in prose** — Prefer “FlowJet” for the product.
2. **Formal CLI** — Document and prefer ``flowjet`` in install guides and packaging.
3. **Aliases** — ``fj`` is a short alias of ``flowjet``; ``fjf`` is a short alias of ``flowjet -f`` (follow this project's latest thread).
4. **Install name** — Always ``pip install flowjet`` / ``uv tool install flowjet``. The HTTP service needs ``pip install 'flowjet[server]'``.
5. **Import path** — Use ``from flowjet.core…``, ``from flowjet.cli…``, ``from flowjet.server…``.
6. **One-liner** — Package / formal CLI: **flowjet** · aliases: **fj**, **fjf** · Service: **flowjet-server** (alias **fj serve**) · Runtime: **soothe-nano**.

## RFC-004 — the 2.0 rename (decision record)

`docs/naming.md` previously froze `flowjet-agent` / `fj_ai` "unless a later RFC
migrates the package directory". **RFC-004 is that RFC.**

**Decision.** Merge `flowjet-server` into this repository as a single
distribution named `flowjet`, with `flowjet.core` (shared runtime),
`flowjet.cli` (was `fj_ai`) and `flowjet.server` (was `flowjet_server`).

**Cutover: hard, no shims.** `fj_ai` and `flowjet_server` are **not** kept as
alias packages. Rationale:

- Two live namespaces for one product means every new symbol is a decision about
  where it goes, and the server would keep a second, divergent copy of the nano
  bootstrap.
- The rename lands together with a major version bump (2.0.0), which is exactly
  the release in which a breaking import change is expected.
- A shim only helps importers; it does not help the two products agree on
  security defaults, session identity, or stream mapping — which was the actual
  motivation for the merge.

**Cost.** Anyone importing `fj_ai.*` or `flowjet_server.*` must update.
Migration table: [upgrade.md](upgrade.md).

**Not changed.** The repository name (`flowjet-agent`), the `fj` / `fjf` aliases,
and the `flowjet-server` console script — so Docker, compose and existing shell
habits keep working.

## Example

```bash
pip install 'flowjet[server]'

flowjet explain this repo
fj explain this repo                    # alias of flowjet
fjf what did we decide last time?       # alias of flowjet -f
flowjet-server                          # HTTP service (ACP + OpenAI-compatible)
fj serve --port 8618                    # same service from the CLI
```
