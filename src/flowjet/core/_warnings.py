"""Suppress noisy third-party warnings that are not actionable for users.

These warnings come from dependencies (``requests``, ``soothe``/``langchain``)
and fire at import time or agent build time. They are version-mismatch or
beta-API notices that the user cannot fix, so we silence them globally at the
earliest point of each entry point (CLI ``main`` and server ``main``).

Call :func:`suppress_known_warnings` before importing the heavy agent stack.
"""

from __future__ import annotations

import warnings


def suppress_known_warnings() -> None:
    """Silence dependency warnings that users cannot act on.

    Safe to call multiple times — ``filterwarnings`` is idempotent for the
    same rule.

    Suppressed:

    * ``RequestsDependencyWarning`` — ``requests`` emits this when the
      installed ``urllib3`` / ``chardet`` / ``charset_normalizer`` versions
      fall outside its supported range. The versions still work; the warning
      is informational only.
    * ``LangChainBetaWarning`` — ``soothe`` uses ``TypeSafeClassifier`` which
      is a beta LangChain API. The API may change, but the warning is noise
      for end users.
    """
    # ``requests`` import-time version-mismatch warning.
    warnings.filterwarnings(
        "ignore",
        category=UserWarning,
        message=r".*urllib3 .* or chardet .* doesn't match a supported version.*",
    )
    # Also match by the concrete warning class when available — covers the
    # exact ``RequestsDependencyWarning`` emitted by ``requests/__init__.py``.
    try:
        from requests.exceptions import RequestsDependencyWarning

        warnings.filterwarnings("ignore", category=RequestsDependencyWarning)
    except Exception:  # pragma: no cover - requests not installed / API changed
        pass

    # ``soothe`` / LangChain beta-API warning from ``TypeSafeClassifier``.
    try:
        from langchain_core._api.beta_decorator import LangChainBetaWarning

        warnings.filterwarnings("ignore", category=LangChainBetaWarning)
    except Exception:  # pragma: no cover - langchain not installed / API changed
        pass
