# FlowJet Server API Design

**Status:** Superseded  
**Superseded by:** [RFC-001: OpenAI-Compatible API Architecture](../specs/RFC-001-openai-compatible-api.md)  
**Date:** 2026-07-31  

---

This draft captured the initial design for an OpenAI Responses–compatible HTTP adapter over soothe-nano.

**Authoritative specification:** [`docs/specs/RFC-001-openai-compatible-api.md`](../specs/RFC-001-openai-compatible-api.md)

RFC-001 formalizes:

* FastAPI HTTP shell
* Reusable `openai_compat` module (OpenAI Responses + projection)
* **Agent Runtime Protocol** (`agent_runtime`) for pluggable backends
* `bridges.nano` mapping from soothe-nano / flowjet-agent stream semantics
* Phase-1 endpoints, SSE lifecycle, `flowjet` namespace, and security invariants (no CoT / prompts / tool args)

Do not extend this draft; update RFC-001 instead.
