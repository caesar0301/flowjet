"""The CLI must stay installable without the ``[server]`` extra.

``pip install flowjet`` pulls one runtime dependency (soothe-nano). FastAPI,
uvicorn, pydantic-settings and agent-client-protocol belong to the server
extra and must never be imported by ``flowjet.cli`` at module scope.
"""

from __future__ import annotations

import subprocess
import sys

# ``pydantic_settings`` is intentionally absent: soothe-nano (our only runtime
# dependency) already imports it, so it is not a signal for this guard.
SERVER_ONLY_MODULES = ("fastapi", "uvicorn", "acp")

_LEAK_CHECK = (
    "import sys;"
    "leaked = [m for m in {mods} if m in sys.modules];"
    "raise SystemExit('leaked: ' + ','.join(leaked) if leaked else 0)"
)


def _run_isolated(imports: str) -> None:
    script = imports + _LEAK_CHECK.format(mods=SERVER_ONLY_MODULES)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cli_import_does_not_pull_server_deps() -> None:
    _run_isolated("import flowjet.cli.cli, flowjet.cli.agent, flowjet.cli.stream;")


def test_core_import_does_not_pull_server_deps() -> None:
    _run_isolated("import flowjet.core, flowjet.core.backends.fake;")


def test_serve_command_is_dispatched_lazily() -> None:
    """``fj serve`` must be imported inside the dispatch branch, not at scope."""
    import inspect

    import flowjet.cli.cli as cli_mod

    # Not bound at module scope — otherwise importing the CLI would pull uvicorn.
    assert "run_serve" not in vars(cli_mod)

    source = inspect.getsource(cli_mod)
    # ...but it is imported inside ``main()``, indented under the serve branch.
    assert "            from flowjet.cli.serve_cmd import run_serve" in source
