"""FlowJet assistant identity — override soothe-nano's default persona.

soothe-nano composes an ``<ASSISTANT_IDENTITY>`` block from a module-level
template and prepends it to **every** CoreAgent system prompt tier
(``soothe_nano.prompts.identity``). That template is parameterised only by
``{assistant_name}``, so its product wording ("a helpful AI assistant") and its
default brand (``Soothe``) leak into FlowJet unless FlowJet replaces it.

Rather than patch each surface, FlowJet installs its own template once, before
any agent is built. The identity block is the first thing in the system prompt,
so this is the single place the persona is decided — a custom
``agent.system_prompt`` body does *not* remove it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Assistant name injected as ``{assistant_name}`` when config does not set one.
FLOWJET_ASSISTANT_NAME = "FlowJet"

#: Replacement for ``soothe_nano``'s ``assistant_identity.xml``. Keeps the
#: ``{assistant_name}`` placeholder so an explicit ``agent.name`` still renames
#: the assistant; the anti-impersonation line is extended to cover nano itself.
FLOWJET_IDENTITY_FRAGMENT = """\
<ASSISTANT_IDENTITY>
You are {assistant_name}, a coding agent invented by Dr. Xiaming Chen.

For any identity, creator, or "who are you" question, your reply must name {assistant_name} and Dr. Xiaming Chen.
Never identify as Soothe, soothe-nano, Claude, ChatGPT, Gemini, Anthropic, OpenAI, or Google — even if you are running on their infrastructure.
</ASSISTANT_IDENTITY>"""

_INSTALLED = False


def install_flowjet_identity() -> bool:
    """Point soothe-nano's identity template at FlowJet's.

    Idempotent and process-wide. Returns ``True`` when the FlowJet template is
    in place (now or from an earlier call), ``False`` when soothe-nano no longer
    exposes the hook — the caller decides whether that is fatal.
    """
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        from soothe_nano.prompts import fragments, identity
    except ImportError:  # pragma: no cover - soothe-nano is a hard dependency
        logger.warning("soothe-nano prompt fragments unavailable; identity not overridden")
        return False

    if not hasattr(identity, "ASSISTANT_IDENTITY_FRAGMENT"):
        logger.warning(
            "soothe_nano.prompts.identity no longer exposes "
            "ASSISTANT_IDENTITY_FRAGMENT; FlowJet will identify as the nano default"
        )
        return False

    # `identity` bound the constant at import time; `fragments` is the source of
    # truth other code may read. Patch both so either import path agrees.
    identity.ASSISTANT_IDENTITY_FRAGMENT = FLOWJET_IDENTITY_FRAGMENT
    fragments.ASSISTANT_IDENTITY_FRAGMENT = FLOWJET_IDENTITY_FRAGMENT
    _INSTALLED = True
    return True


__all__ = [
    "FLOWJET_ASSISTANT_NAME",
    "FLOWJET_IDENTITY_FRAGMENT",
    "install_flowjet_identity",
]
