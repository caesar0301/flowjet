"""Unit tests for ``fj doctor-fix``."""

from __future__ import annotations

from flowjet.cli import doctor_fix_cmd as dfc
from flowjet.cli.cli import parse_args


def test_parse_args_doctor_fix_command() -> None:
    args = parse_args(["doctor-fix", "--no-color"])
    assert args.command == "doctor-fix"
    assert args.doctor_fix_argv == ["--no-color"]


def test_extract_version() -> None:
    assert dfc._extract_version("Google Chrome 151.0.7922.138") == "151.0.7922.138"
    assert dfc._extract_version("ChromeDriver 150.0.7871.187 (30f6543...)") == "150.0.7871.187"
    assert dfc._extract_version("no version here") is None


def test_major() -> None:
    assert dfc._major("151.0.7922.138") == "151"
    assert dfc._major("150.0.7871.187") == "150"


def test_chromedriver_platform_darwin_arm64(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert dfc._chromedriver_platform() == "mac-arm64"


def test_chromedriver_platform_linux(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert dfc._chromedriver_platform() == "linux64"


def test_resolve_chromedriver_version_exact(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(dfc, "_url_exists", lambda _url: True)
    assert dfc._resolve_chromedriver_version("151.0.0.1", "151", "mac-arm64") == "151.0.0.1"


def test_resolve_chromedriver_version_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(dfc, "_url_exists", lambda _url: False)
    monkeypatch.setattr(
        dfc,
        "_fetch_json",
        lambda _url: {
            "versions": [
                {
                    "version": "150.0.0.1",
                    "downloads": {"chromedriver": [{"platform": "mac-x64"}]},
                },
                {
                    "version": "151.0.0.2",
                    "downloads": {"chromedriver": [{"platform": "mac-arm64"}]},
                },
            ]
        },
    )
    assert dfc._resolve_chromedriver_version("151.0.0.1", "151", "mac-arm64") == "151.0.0.2"


def test_run_doctor_fix_already_matching(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(dfc, "_fix_reference_skills", lambda use_color=False: 0)
    monkeypatch.setattr(dfc, "_fix_anydoc", lambda use_color=False: 0)
    monkeypatch.setattr(dfc, "_resolve_chrome", lambda use_color=False: ("151.0.0.1", "151"))
    monkeypatch.setattr(dfc, "_resolve_chromedriver", lambda: ("151.0.0.1", "151"))

    assert dfc.run_doctor_fix(["--no-color"]) == 0
    out = capsys.readouterr().out
    assert "match exactly" in out


def test_run_doctor_fix_downloads_mismatch(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(dfc, "_fix_reference_skills", lambda use_color=False: 0)
    monkeypatch.setattr(dfc, "_fix_anydoc", lambda use_color=False: 0)
    monkeypatch.setattr(dfc, "_resolve_chrome", lambda use_color=False: ("151.0.0.1", "151"))
    state = {"calls": 0}

    def fake_resolve_chromedriver():
        state["calls"] += 1
        return None if state["calls"] == 1 else ("151.0.0.1", "151")

    monkeypatch.setattr(dfc, "_resolve_chromedriver", fake_resolve_chromedriver)
    monkeypatch.setattr(dfc, "_chromedriver_platform", lambda: "mac-arm64")
    monkeypatch.setattr(dfc, "_resolve_chromedriver_version", lambda v, m, p: "151.0.0.1")
    monkeypatch.setattr(dfc, "_download_chromedriver", lambda v, p, d: True)

    assert dfc.run_doctor_fix(["--no-color"]) == 0
    out = capsys.readouterr().out
    assert "Downloading chromedriver" in out
    assert "major versions match" in out


def test_run_doctor_fix_chrome_missing_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(dfc, "_fix_reference_skills", lambda use_color=False: 0)
    monkeypatch.setattr(dfc, "_fix_anydoc", lambda use_color=False: 0)
    monkeypatch.setattr(dfc, "_resolve_chrome", lambda use_color=False: None)

    assert dfc.run_doctor_fix(["--no-color"]) == 1


def test_fix_reference_skills_all_present(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        dfc,
        "_installed_skill_names",
        lambda: {"oh-my-research", "deep-research", "autoresearch"},
    )
    assert dfc._fix_reference_skills(use_color=False) == 0


def test_fix_anydoc_already_present(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(dfc, "_find_anydoc", lambda: "/usr/local/bin/anydoc")
    assert dfc._fix_anydoc(use_color=False) == 0
    assert "anydoc installed" in capsys.readouterr().out


def test_fix_anydoc_installs_missing(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    state = {"calls": 0}

    def fake_find_anydoc():
        state["calls"] += 1
        return None if state["calls"] == 1 else "/usr/local/bin/anydoc"

    monkeypatch.setattr(dfc, "_find_anydoc", fake_find_anydoc)
    monkeypatch.setattr(dfc.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(dfc.subprocess, "run", lambda *a, **k: None)

    assert dfc._fix_anydoc(use_color=False) == 0
    out = capsys.readouterr().out
    assert "Installing @firecrawl/anydoc" in out
    assert "anydoc installed" in out


def test_fix_anydoc_no_npm(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(dfc, "_find_anydoc", lambda: None)
    monkeypatch.setattr(dfc.shutil, "which", lambda name: None)

    assert dfc._fix_anydoc(use_color=False) == 1
    assert "npm not found" in capsys.readouterr().err


def test_fix_reference_skills_installs_missing(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    state = {"installed": set(), "calls": []}

    def fake_installed():
        return set(state["installed"])

    def fake_install(repo):
        state["calls"].append(repo)
        # Simulate the skill name landing after install for the first two.
        if repo == "caesar0301/oh-my-research":
            state["installed"].add("oh-my-research")
        if repo == "uditgoenka/autoresearch":
            state["installed"].add("autoresearch")
        return True, f"installed {repo}"

    monkeypatch.setattr(dfc, "_installed_skill_names", fake_installed)
    monkeypatch.setattr(dfc, "_install_skill", fake_install)

    # academic-research-skills (deep-research) never lands in the mock -> install
    # "succeeds" but the re-scan finds nothing, so it is reported as a failure.
    assert dfc._fix_reference_skills(use_color=False) == 1
    captured = capsys.readouterr()
    assert "oh-my-research" in captured.out
    assert "Imbad0202/academic-research-skills" in captured.err
    assert state["calls"] == [
        "caesar0301/oh-my-research",
        "Imbad0202/academic-research-skills",
        "uditgoenka/autoresearch",
    ]
