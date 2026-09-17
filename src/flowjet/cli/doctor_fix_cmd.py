"""``fj doctor-fix`` — repair runtime deps and install missing reference skills.

Two repairs:
1. Reference skills — install missing research-bootstrap backends
   (oh-my-research, academic-research-skills, autoresearch) under
   ``~/.agents/skills`` via ``npx skills add`` non-interactively.
2. Chrome/chromedriver — mirror ``soothe/scripts/check_chrome.sh``: detect the
   installed Chrome/Chromium major version, then install Chrome (Homebrew) or
   download a matching chromedriver (Chrome-for-Testing) when either is missing
   or the major versions diverge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from typing import Any

from flowjet.cli.doctor_cmd import (
    REFERENCE_SKILLS,
    _bin_version,
    _find_anydoc,
    _find_chrome_executable,
    _find_chromedriver,
    _install_skill,
    _installed_skill_names,
)

CHROME_FOR_TESTING_BASE = "https://storage.googleapis.com/chrome-for-testing-public"
KNOWN_GOOD_VERSIONS_JSON = (
    "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
)

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

_RED = "\033[0;31m"
_GREEN = "\033[0;32m"
_YELLOW = "\033[0;33m"
_NC = "\033[0m"


def _paint(code: str, text: str, *, use_color: bool) -> str:
    return f"{code}{text}{_NC}" if use_color else text


def _info(msg: str, *, use_color: bool) -> None:
    sys.stdout.write(_paint(_YELLOW, f"[INFO] {msg}", use_color=use_color) + "\n")


def _ok(msg: str, *, use_color: bool) -> None:
    sys.stdout.write(_paint(_GREEN, f"[OK]   {msg}", use_color=use_color) + "\n")


def _err(msg: str, *, use_color: bool) -> None:
    sys.stderr.write(_paint(_RED, f"[ERR]  {msg}", use_color=use_color) + "\n")


def _extract_version(text: str) -> str | None:
    match = _VERSION_RE.search(text)
    return match.group(0) if match else None


def _major(version: str) -> str:
    return version.split(".", 1)[0]


def _chromedriver_platform() -> str | None:
    """Map the current OS/arch onto a Chrome-for-Testing platform key."""
    import platform

    system = platform.system()
    machine = platform.machine()
    if system == "Darwin":
        return "mac-arm64" if machine == "arm64" else "mac-x64"
    if system == "Linux":
        return "linux64"
    return None


def _ssl_context() -> ssl.SSLContext:
    """Build an SSL context pinned to certifi's CA bundle when available.

    Some environments (e.g. a Homebrew Python whose ``openssl@3`` CA store is
    empty) fail default verification even though ``curl`` works. certifi ships
    the public CAs that Chrome-for-Testing storage is signed against.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _open_url(url: str, *, method: str = "GET", timeout: int = 15) -> Any:
    request = urllib.request.Request(url, method=method)
    return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context())


def _url_exists(url: str) -> bool:
    try:
        with _open_url(url, method="HEAD", timeout=10) as resp:
            return bool(resp.status < 400)
    except (urllib.error.HTTPError, OSError):
        return False


def _fetch_json(url: str) -> Any:
    with _open_url(url) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_chromedriver_version(
    chrome_version: str,
    chrome_major: str,
    platform: str,
) -> str | None:
    """Resolve a chromedriver version matching ``chrome_major`` on ``platform``."""
    exact_url = f"{CHROME_FOR_TESTING_BASE}/{chrome_version}/{platform}/chromedriver-{platform}.zip"
    if _url_exists(exact_url):
        return chrome_version

    try:
        data = _fetch_json(KNOWN_GOOD_VERSIONS_JSON)
    except (OSError, ValueError):
        return None

    for entry in data.get("versions", []):
        version = entry.get("version", "")
        if not version.startswith(f"{chrome_major}."):
            continue
        for download in entry.get("downloads", {}).get("chromedriver", []):
            if download.get("platform") == platform:
                return str(version)
    return None


def _download_chromedriver(version: str, platform: str, dest_dir: str) -> bool:
    """Download and install chromedriver ``version`` for ``platform`` into ``dest_dir``."""
    url = f"{CHROME_FOR_TESTING_BASE}/{version}/{platform}/chromedriver-{platform}.zip"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "chromedriver.zip")
            with _open_url(url) as resp, open(zip_path, "wb") as handle:
                handle.write(resp.read())
            with zipfile.ZipFile(zip_path) as archive:
                member = next(
                    (
                        name
                        for name in archive.namelist()
                        if name.rstrip("/").endswith("/chromedriver")
                    ),
                    None,
                )
                if member is None:
                    return False
                payload = archive.read(member)

            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, "chromedriver")
            with open(dest, "wb") as handle:
                handle.write(payload)
            os.chmod(dest, 0o755)
        return True
    except (OSError, zipfile.BadZipFile):
        return False


def _install_chrome(*, use_color: bool) -> bool:
    if shutil.which("brew") is None:
        _err(
            "Homebrew is required to install Google Chrome automatically "
            "(brew install --cask google-chrome).",
            use_color=use_color,
        )
        return False
    _info("Installing Google Chrome via Homebrew...", use_color=use_color)
    try:
        subprocess.run(["brew", "install", "--cask", "google-chrome"], check=True)
    except (OSError, subprocess.CalledProcessError):
        _err("Failed to install Google Chrome via Homebrew.", use_color=use_color)
        return False
    return True


def _resolve_chrome(*, use_color: bool) -> tuple[str, str] | None:
    """Return ``(version, major)`` for an installed Chrome/Chromium, installing it first.

    Returns ``None`` when Chrome cannot be found or its version cannot be read.
    """
    exe = _find_chrome_executable()
    if exe is None:
        _err("Google Chrome / Chromium not found.", use_color=use_color)
        if not _install_chrome(use_color=use_color):
            return None
        exe = _find_chrome_executable()
        if exe is None:
            _err("Installed Chrome but still cannot find it.", use_color=use_color)
            return None
    version = _extract_version(_bin_version(exe) or "")
    if version is None:
        _err(f"Could not determine Chrome version from: {exe}", use_color=use_color)
        return None
    return version, _major(version)


def _resolve_chromedriver() -> tuple[str, str] | None:
    """Return ``(version, major)`` for an installed chromedriver, or ``None``."""
    exe = _find_chromedriver()
    if exe is None:
        return None
    version = _extract_version(_bin_version(exe) or "")
    if version is None:
        return None
    return version, _major(version)


def _fix_reference_skills(*, use_color: bool) -> int:
    """Install missing research-bootstrap reference skills under ``~/.agents/skills``.

    Returns 0 when all three are present (or installed), 1 if any could not be
    resolved after install.
    """
    installed = _installed_skill_names()
    all_ok = True
    for entry in REFERENCE_SKILLS:
        repo = str(entry["repo"])
        expected = tuple(entry["skills"])
        if any(name in installed for name in expected):
            _ok(f"{repo} installed ({', '.join(expected)})", use_color=use_color)
            continue
        _info(f"Installing {repo} via 'npx skills add'...", use_color=use_color)
        _, detail = _install_skill(repo)
        # Verify by actual landing under ~/.agents/skills, not the CLI exit code:
        # a non-zero exit can result from unrelated agent-symlink failures even
        # when the universal install succeeded.
        installed = _installed_skill_names()
        if any(name in installed for name in expected):
            _ok(f"{repo} installed", use_color=use_color)
        else:
            _err(f"{repo}: {detail}", use_color=use_color)
            all_ok = False
    return 0 if all_ok else 1


def _fix_anydoc(*, use_color: bool) -> int:
    """Install the global ``@firecrawl/anydoc`` CLI when the ``anydoc`` binary is missing.

    Returns 0 when ``anydoc`` is present (or installed), 1 if it could not be
    resolved after install.
    """
    if _find_anydoc():
        _ok("anydoc installed", use_color=use_color)
        return 0
    npm = shutil.which("npm")
    if npm is None:
        _err(
            "npm not found on PATH (install Node.js to auto-install anydoc)",
            use_color=use_color,
        )
        return 1
    _info("Installing @firecrawl/anydoc via 'npm install -g'...", use_color=use_color)
    try:
        subprocess.run([npm, "install", "-g", "@firecrawl/anydoc"], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        _err(f"npm install -g @firecrawl/anydoc failed: {exc}", use_color=use_color)
        return 1
    if _find_anydoc():
        _ok("anydoc installed", use_color=use_color)
        return 0
    _err("Installed @firecrawl/anydoc but still cannot find it on PATH.", use_color=use_color)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    from flowjet.cli.cli import resolve_cli_prog

    parser = argparse.ArgumentParser(
        prog=f"{resolve_cli_prog()} doctor-fix",
        description="Install missing reference skills + repair Chrome/chromedriver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in output",
    )
    return parser


def run_doctor_fix(argv: list[str] | None = None) -> int:
    """Entry point for ``fj doctor-fix``."""
    args = _build_parser().parse_args(list(argv or []))
    use_color = not args.no_color and sys.stdout.isatty()

    skills_code = _fix_reference_skills(use_color=use_color)
    anydoc_code = _fix_anydoc(use_color=use_color)

    chrome = _resolve_chrome(use_color=use_color)
    if chrome is None:
        return 1
    chrome_version, chrome_major = chrome
    _ok(f"Chrome version: {chrome_version} (major {chrome_major})", use_color=use_color)

    chromedriver = _resolve_chromedriver()
    need_download = chromedriver is None
    if chromedriver is not None:
        driver_version, driver_major = chromedriver
        _ok(
            f"chromedriver version: {driver_version} (major {driver_major})",
            use_color=use_color,
        )
        if driver_major != chrome_major:
            _err(
                f"Version mismatch: Chrome major={chrome_major}, "
                f"chromedriver major={driver_major}.",
                use_color=use_color,
            )
            need_download = True
        elif driver_version != chrome_version:
            _info(
                "Major versions match but exact versions differ; downloading exact match...",
                use_color=use_color,
            )
            need_download = True
        else:
            _ok("Chrome and chromedriver match exactly; nothing to fix.", use_color=use_color)
    else:
        _info("chromedriver not found; downloading a matching version...", use_color=use_color)

    if need_download:
        platform = _chromedriver_platform()
        if platform is None:
            _err(
                "Unsupported OS for chromedriver download (macOS/Linux only).", use_color=use_color
            )
            return 1
        version = _resolve_chromedriver_version(chrome_version, chrome_major, platform)
        if version is None:
            _err(
                f"No chromedriver build found for Chrome major {chrome_major} on {platform}.",
                use_color=use_color,
            )
            return 1
        dest_dir = os.environ.get(
            "CHROMEDRIVER_DIR",
            os.path.join(os.path.expanduser("~"), ".local", "bin"),
        )
        _info(f"Downloading chromedriver {version} ({platform})...", use_color=use_color)
        if not _download_chromedriver(version, platform, dest_dir):
            _err("Failed to download/install chromedriver.", use_color=use_color)
            return 1
        _ok(
            f"Installed chromedriver to {os.path.join(dest_dir, 'chromedriver')}",
            use_color=use_color,
        )

    chromedriver = _resolve_chromedriver()
    if chromedriver is None:
        _err("Installed chromedriver but could not determine its version.", use_color=use_color)
        return 1
    driver_version, driver_major = chromedriver
    if driver_major == chrome_major:
        _ok(
            f"Chrome and chromedriver major versions match ({chrome_major}).",
            use_color=use_color,
        )
        return 1 if (skills_code or anydoc_code) else 0
    _err(
        f"chromedriver major {driver_major} still does not match Chrome major {chrome_major}.",
        use_color=use_color,
    )
    return 1
