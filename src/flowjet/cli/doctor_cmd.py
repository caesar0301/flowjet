"""``fj doctor`` — progressive diagnosis via soothe-nano diagnose API."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO

_STATUS_SYMBOLS = {
    "ok": "✓",
    "warning": "⚠",
    "error": "✗",
    "info": "ℹ",  # noqa: RUF001
    "skipped": "○",
}

_STATUS_PLAIN = {
    "ok": "[OK]",
    "warning": "[WARN]",
    "error": "[ERROR]",
    "info": "[INFO]",
    "skipped": "[SKIP]",
}

_SEVERITY = {
    "ok": 0,
    "info": 1,
    "skipped": 2,
    "warning": 3,
    "error": 4,
}

# Reference skills required by the ``research-bootstrap`` builtin skill. Each
# entry maps a ``npx skills add <repo>`` argument to the SKILL.md ``name:``
# value(s) that indicate it is installed under ``~/.agents/skills/``.
REFERENCE_SKILLS: tuple[dict[str, Any], ...] = (
    {"repo": "caesar0301/oh-my-research", "skills": ("oh-my-research",)},
    {"repo": "Imbad0202/academic-research-skills", "skills": ("deep-research",)},
    {"repo": "uditgoenka/autoresearch", "skills": ("autoresearch",)},
)


def _build_parser() -> argparse.ArgumentParser:
    from flowjet.cli.agent import default_config_path
    from flowjet.cli.cli import resolve_cli_prog

    parser = argparse.ArgumentParser(
        prog=f"{resolve_cli_prog()} doctor",
        description="Diagnose FlowJet / soothe-nano runtime readiness (tool deps, providers, …)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help=f"Alternate nano.yml (default: {default_config_path()})",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Include deep nano categories (MCP, vector stores, models, protocols)",
    )
    parser.add_argument(
        "--live-llm",
        action="store_true",
        help="Live-invoke the default router model (may call the provider)",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("text", "json"),
        default="text",
        help="Report format (default: text progressive)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in text output",
    )
    parser.add_argument(
        "--fail-on",
        choices=("error", "warning"),
        default="error",
        help="Exit non-zero when status reaches this severity (default: error)",
    )
    return parser


def parse_doctor_args(argv: list[str]) -> argparse.Namespace:
    """Parse ``fj doctor`` argv (tokens after the ``doctor`` word)."""
    return _build_parser().parse_args(argv)


def _worst_status(categories: list[dict[str, Any]]) -> str:
    worst = "ok"
    for cat in categories:
        status = str(cat.get("status", "ok"))
        if _SEVERITY.get(status, 0) > _SEVERITY.get(worst, 0):
            worst = status
    return worst


def _exit_code(overall: str, *, fail_on: str) -> int:
    if fail_on == "warning":
        return 1 if _SEVERITY.get(overall, 0) >= _SEVERITY["warning"] else 0
    return 1 if overall == "error" else 0


def _symbol(status: str, *, use_color: bool) -> str:
    table = _STATUS_SYMBOLS if use_color else _STATUS_PLAIN
    return table.get(status, status)


def _print_category(cat: dict[str, Any], *, use_color: bool, stream: TextIO) -> None:
    status = str(cat.get("status", "ok"))
    title = str(cat.get("category", "unknown")).replace("_", " ").title()
    stream.write(f"{_symbol(status, use_color=use_color)} {title}\n")
    for check in cat.get("checks") or []:
        if not isinstance(check, dict):
            continue
        cstatus = str(check.get("status", "ok"))
        msg = str(check.get("message", ""))
        stream.write(f"  {_symbol(cstatus, use_color=use_color)} {msg}\n")
        details = check.get("details") or {}
        if cstatus in ("error", "warning") and isinstance(details, dict):
            for key in ("impact", "remediation"):
                if key in details:
                    stream.write(f"    └─ {key.title()}: {details[key]}\n")
    stream.write("\n")
    stream.flush()


def _print_summary(overall: str, *, use_color: bool, stream: TextIO) -> None:
    stream.write("━" * 60 + "\n")
    stream.write(f"Overall Status: {_symbol(overall, use_color=use_color)} {overall.upper()}\n")


async def _run_diagnose(
    config: Any | None,
    *,
    deep: bool,
    live_llm: bool,
) -> list[dict[str, Any]]:
    try:
        from soothe_nano.diagnose import diagnose
    except ImportError as exc:
        raise RuntimeError(
            "soothe-nano diagnose API unavailable; upgrade soothe-nano "
            "(fj doctor requires soothe-nano>=1.0.8)"
        ) from exc
    return await diagnose(config, deep=deep, live_llm=live_llm)


def _bin_version(bin_path: str) -> str | None:
    """Return first line of ``--version`` output, or None on failure."""
    try:
        result = subprocess.run(  # noqa: S603
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        line = (result.stdout or result.stderr or "").strip().splitlines()
        return line[0].strip() if line else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _find_chrome_executable() -> str | None:
    """Locate a Chrome/Chromium-family browser executable, or None."""
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif sys.platform.startswith("win"):
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        ]
    else:
        candidates = []

    for path in candidates:
        if os.path.isfile(path):
            return path

    # Fall back to PATH lookup (also covers Linux package installs).
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "brave-browser",
        "chrome",
        "chrome.exe",
    ):
        found = shutil.which(name)
        if found:
            return found
    return None


def _find_chromedriver() -> str | None:
    """Locate chromedriver on PATH or in common install locations, or None."""
    found = shutil.which("chromedriver")
    if found:
        return found

    candidates = [
        os.path.expanduser("~/.local/bin/chromedriver"),
        "/usr/local/bin/chromedriver",
        "/usr/bin/chromedriver",
        os.path.expanduser("~/chromedriver/chromedriver"),
    ]
    if sys.platform == "darwin":
        candidates.append("/opt/homebrew/bin/chromedriver")
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _check_browser_deps() -> dict[str, Any]:
    """Build the ``browser`` diagnose category (Chrome/Chromium + chromedriver).

    ``soothe-nano`` pulls in ``tarzi`` (headless-browser crawl fallback) and a
    ``browser_use`` subagent, both of which need a Chrome/Chromium binary that
    upstream ``soothe_nano.diagnose`` does not check for.
    """
    checks: list[dict[str, Any]] = []

    chrome = _find_chrome_executable()
    if chrome:
        version = _bin_version(chrome)
        details: dict[str, Any] = {"path": chrome}
        if version:
            details["version"] = version
        msg = f"chrome available: {chrome}"
        if version:
            msg += f" ({version})"
        checks.append({"name": "chrome", "status": "ok", "message": msg, "details": details})
    else:
        checks.append(
            {
                "name": "chrome",
                "status": "warning",
                "message": "Chrome/Chromium not found",
                "details": {
                    "impact": (
                        "browser_use subagent and tarzi headless crawl will fail at runtime"
                    ),
                    "remediation": "Install Google Chrome or Chromium",
                },
            }
        )

    chromedriver = _find_chromedriver()
    if chromedriver:
        version = _bin_version(chromedriver)
        details = {"path": chromedriver}
        if version:
            details["version"] = version
        msg = f"chromedriver available: {chromedriver}"
        if version:
            msg += f" ({version})"
        checks.append({"name": "chromedriver", "status": "ok", "message": msg, "details": details})
    else:
        checks.append(
            {
                "name": "chromedriver",
                "status": "warning",
                "message": "chromedriver not found",
                "details": {
                    "impact": "browser automation via chromedriver will fail",
                    "remediation": "Install chromedriver (e.g. brew install --cask chromedriver)",
                },
            }
        )

    worst = "ok"
    for check in checks:
        if _SEVERITY.get(str(check["status"]), 0) > _SEVERITY.get(worst, 0):
            worst = str(check["status"])

    return {
        "category": "browser",
        "status": worst,
        "checks": checks,
        "message": None,
    }


def _agents_skills_dir() -> Path:
    """Return the cross-tool Agent Skills directory (``~/.agents/skills``)."""
    return Path(os.path.expanduser("~/.agents/skills"))


def _installed_skill_names() -> set[str]:
    """Collect ``name:`` frontmatter values from every ``SKILL.md`` under
    ``~/.agents/skills/`` (one level deep — ``~/.agents/skills/<skill>/SKILL.md``).
    """
    root = _agents_skills_dir()
    names: set[str] = set()
    if not root.is_dir():
        return names
    for skill_md in root.glob("*/SKILL.md"):
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines()[:30]:
            stripped = line.strip()
            if stripped.lower().startswith("name:"):
                value = stripped.split(":", 1)[1].strip().strip("\"'")
                if value:
                    names.add(value)
                break
    return names


def _install_skill(repo: str) -> tuple[bool, str]:
    """Install ``repo`` via ``npx skills add`` non-interactively.

    Returns ``(ok, detail)``. Non-interactive: ``npx -y`` auto-confirms npx's
    own package install; ``skills add --all --global --yes`` installs every
    skill in the repo (``--all``, required for multi-skill repos like
    academic-research-skills), into ``~/.agents/skills/`` (``--global``), with
    prompts suppressed (``--yes``). ``--all`` also targets every supported agent
    (some of which reject global installs and yield a non-zero exit); callers
    must verify by actual landing under ``~/.agents/skills/``, not the exit code.
    """
    npx = shutil.which("npx")
    if npx is None:
        return False, "npx not found on PATH (install Node.js to auto-install skills)"
    try:
        result = subprocess.run(  # noqa: S603
            [npx, "-y", "skills", "add", repo, "--all", "--global", "--yes"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"install failed: {exc}"
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, f"npx skills add failed: {tail[-1] if tail else 'unknown error'}"
    return True, f"installed {repo}"


def _check_reference_skills() -> dict[str, Any]:
    """Build the ``reference_skills`` diagnose category (read-only).

    Verifies the research-bootstrap backends (oh-my-research, academic-research-skills,
    autoresearch) are installed under ``~/.agents/skills``. Installation is handled by
    ``fj doctor-fix``, not here.
    """
    installed = _installed_skill_names()
    checks: list[dict[str, Any]] = []
    for entry in REFERENCE_SKILLS:
        repo = str(entry["repo"])
        expected = tuple(entry["skills"])
        present = any(name in installed for name in expected)
        if present:
            checks.append(
                {
                    "name": repo,
                    "status": "ok",
                    "message": f"{repo} installed ({', '.join(expected)})",
                    "details": {"path": str(_agents_skills_dir())},
                }
            )
        else:
            checks.append(
                {
                    "name": repo,
                    "status": "warning",
                    "message": f"{repo} not installed",
                    "details": {
                        "remediation": f"npx -y skills add {repo} --all --global --yes",
                        "impact": "research-bootstrap backend unavailable",
                    },
                }
            )

    worst = "ok"
    for check in checks:
        if _SEVERITY.get(str(check["status"]), 0) > _SEVERITY.get(worst, 0):
            worst = str(check["status"])
    return {
        "category": "reference_skills",
        "status": worst,
        "checks": checks,
        "message": None,
    }


def _find_anydoc() -> str | None:
    """Locate the ``anydoc`` CLI (global ``@firecrawl/anydoc`` install), or None."""
    return shutil.which("anydoc")


def _check_anydoc() -> dict[str, Any]:
    """Build the ``anydoc`` diagnose category (document-to-markdown converter).

    The ``anydoc`` builtin skill shells out to ``@firecrawl/anydoc`` to convert
    office/PDF/ebook files to Markdown. The globally-installed ``anydoc`` CLI is
    the fastest path (no per-run ``npx`` fetch); ``fj doctor-fix`` installs it
    via ``npm install -g @firecrawl/anydoc``.
    """
    anydoc = _find_anydoc()
    if anydoc:
        version = _bin_version(anydoc)
        details: dict[str, Any] = {"path": anydoc}
        if version:
            details["version"] = version
        msg = f"anydoc available: {anydoc}"
        if version:
            msg += f" ({version})"
        checks = [{"name": "anydoc", "status": "ok", "message": msg, "details": details}]
    else:
        checks = [
            {
                "name": "anydoc",
                "status": "warning",
                "message": "anydoc not found",
                "details": {
                    "impact": "document-to-markdown conversion (office/PDF/ebook) unavailable",
                    "remediation": "npm install -g @firecrawl/anydoc",
                },
            }
        ]
    return {"category": "anydoc", "status": checks[0]["status"], "checks": checks, "message": None}


def run_doctor(argv: list[str] | None = None) -> int:
    """Entry point for ``fj doctor`` (sync wrapper around async diagnose)."""
    import asyncio

    args = parse_doctor_args(list(argv or []))
    try:
        return asyncio.run(_run_doctor_async(args))
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130


async def _run_doctor_async(args: argparse.Namespace) -> int:
    from flowjet.cli.agent import load_config

    config: Any | None
    try:
        config = load_config(getattr(args, "config", None))
    except Exception as exc:
        sys.stderr.write(f"warning: config load failed ({exc}); running limited checks\n")
        config = None

    try:
        categories = await _run_diagnose(
            config,
            deep=bool(args.deep),
            live_llm=bool(args.live_llm),
        )
    except RuntimeError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"error: diagnose failed: {exc}\n")
        return 1

    categories = list(categories) + [
        _check_browser_deps(),
        _check_reference_skills(),
        _check_anydoc(),
    ]
    overall = _worst_status(categories)
    use_color = not bool(args.no_color) and sys.stdout.isatty()

    if args.output_format == "json":
        payload = {
            "overall_status": overall,
            "categories": categories,
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write("fj doctor — progressive diagnosis\n")
        sys.stdout.write("━" * 60 + "\n\n")
        sys.stdout.flush()
        for cat in categories:
            _print_category(cat, use_color=use_color, stream=sys.stdout)
        _print_summary(overall, use_color=use_color, stream=sys.stdout)

    return _exit_code(overall, fail_on=str(args.fail_on))
