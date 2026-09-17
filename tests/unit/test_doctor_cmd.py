"""Unit tests for ``fj doctor``."""

from __future__ import annotations

from types import SimpleNamespace

from flowjet.cli.cli import parse_args
from flowjet.cli.doctor_cmd import parse_doctor_args, run_doctor


def _stub_browser_ok() -> dict:
    return {"category": "browser", "status": "ok", "checks": [], "message": None}


def _stub_reference_skills_ok() -> dict:
    return {"category": "reference_skills", "status": "ok", "checks": [], "message": None}


def _stub_anydoc_ok() -> dict:
    return {"category": "anydoc", "status": "ok", "checks": [], "message": None}


def test_parse_args_doctor_command() -> None:
    args = parse_args(["doctor", "--deep", "--live-llm"])
    assert args.command == "doctor"
    assert args.doctor_argv == ["--deep", "--live-llm"]


def test_parse_doctor_args_defaults() -> None:
    args = parse_doctor_args([])
    assert args.deep is False
    assert args.live_llm is False
    assert args.output_format == "text"
    assert args.fail_on == "error"


def test_main_doctor_dispatches(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import cli

    called: list[list[str]] = []

    monkeypatch.setattr(cli, "configure_cli_logging", lambda **_k: None)
    monkeypatch.setattr(
        "flowjet.cli.doctor_cmd.run_doctor",
        lambda argv: called.append(list(argv)) or 0,
    )

    def boom(*_a: object, **_k: object) -> int:
        raise AssertionError("doctor must not use asyncio.run in main")

    monkeypatch.setattr(cli.asyncio, "run", boom)
    assert cli.main(["doctor", "--deep"]) == 0
    assert called == [["--deep"]]


def test_run_doctor_json(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    async def fake_diagnose(_config=None, **kwargs):
        assert kwargs.get("deep") is False
        return [
            {
                "category": "tool_deps",
                "status": "ok",
                "checks": [{"name": "rg", "status": "ok", "message": "rg ok", "details": {}}],
                "message": None,
            }
        ]

    monkeypatch.setattr("flowjet.cli.doctor_cmd._run_diagnose", fake_diagnose)
    monkeypatch.setattr("flowjet.cli.doctor_cmd._check_browser_deps", _stub_browser_ok)
    monkeypatch.setattr(
        "flowjet.cli.doctor_cmd._check_reference_skills", lambda **_k: _stub_reference_skills_ok()
    )
    monkeypatch.setattr("flowjet.cli.doctor_cmd._check_anydoc", lambda: _stub_anydoc_ok())
    monkeypatch.setattr(
        "flowjet.cli.agent.load_config",
        lambda _path=None: SimpleNamespace(),
    )
    assert run_doctor(["--format", "json", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert '"overall_status": "ok"' in out
    assert "tool_deps" in out
    assert "browser" in out


def test_run_doctor_progressive_text(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    async def fake_diagnose(_config=None, **_kwargs):
        return [
            {
                "category": "providers",
                "status": "warning",
                "checks": [
                    {
                        "name": "openai",
                        "status": "warning",
                        "message": "api key missing",
                        "details": {"remediation": "set OPENAI_API_KEY"},
                    }
                ],
                "message": None,
            }
        ]

    monkeypatch.setattr("flowjet.cli.doctor_cmd._run_diagnose", fake_diagnose)
    monkeypatch.setattr("flowjet.cli.doctor_cmd._check_browser_deps", _stub_browser_ok)
    monkeypatch.setattr(
        "flowjet.cli.doctor_cmd._check_reference_skills", lambda **_k: _stub_reference_skills_ok()
    )
    monkeypatch.setattr("flowjet.cli.doctor_cmd._check_anydoc", lambda: _stub_anydoc_ok())
    monkeypatch.setattr(
        "flowjet.cli.agent.load_config",
        lambda _path=None: SimpleNamespace(),
    )
    code = run_doctor(["--no-color", "--fail-on", "warning"])
    assert code == 1
    out = capsys.readouterr().out
    assert "progressive diagnosis" in out
    assert "Providers" in out
    assert "api key missing" in out
    assert "Remediation: set OPENAI_API_KEY" in out


def test_run_doctor_missing_diagnose_api(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    async def boom(_config=None, **_kwargs):
        raise RuntimeError("soothe-nano diagnose API unavailable")

    monkeypatch.setattr("flowjet.cli.doctor_cmd._run_diagnose", boom)
    monkeypatch.setattr(
        "flowjet.cli.agent.load_config",
        lambda _path=None: SimpleNamespace(),
    )
    assert run_doctor(["--no-color"]) == 1
    assert "diagnose API unavailable" in capsys.readouterr().err


def test_check_browser_deps_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import doctor_cmd

    monkeypatch.setattr(doctor_cmd, "_find_chrome_executable", lambda: None)
    monkeypatch.setattr(doctor_cmd, "_find_chromedriver", lambda: None)

    cat = doctor_cmd._check_browser_deps()
    assert cat["category"] == "browser"
    assert cat["status"] == "warning"
    assert {c["name"] for c in cat["checks"]} == {"chrome", "chromedriver"}
    assert all(c["status"] == "warning" for c in cat["checks"])


def test_check_browser_deps_present(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import doctor_cmd

    monkeypatch.setattr(doctor_cmd, "_find_chrome_executable", lambda: "/usr/bin/chrome")
    monkeypatch.setattr(doctor_cmd, "_find_chromedriver", lambda: "/usr/bin/chromedriver")
    monkeypatch.setattr(doctor_cmd, "_bin_version", lambda _path: None)

    cat = doctor_cmd._check_browser_deps()
    assert cat["status"] == "ok"
    assert all(c["status"] == "ok" for c in cat["checks"])


def test_find_chrome_executable_uses_path(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import doctor_cmd

    monkeypatch.setattr(doctor_cmd.sys, "platform", "linux")
    monkeypatch.setattr(doctor_cmd.os.path, "isfile", lambda _p: False)
    monkeypatch.setattr(
        doctor_cmd.shutil, "which", lambda name: "/usr/bin/chromium" if name == "chromium" else None
    )

    assert doctor_cmd._find_chrome_executable() == "/usr/bin/chromium"


def test_check_reference_skills_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import doctor_cmd

    monkeypatch.setattr(doctor_cmd, "_installed_skill_names", lambda: set())

    cat = doctor_cmd._check_reference_skills()
    assert cat["category"] == "reference_skills"
    assert cat["status"] == "warning"
    assert len(cat["checks"]) == len(doctor_cmd.REFERENCE_SKILLS)
    assert all(c["status"] == "warning" for c in cat["checks"])


def test_check_reference_skills_installed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import doctor_cmd

    monkeypatch.setattr(
        doctor_cmd,
        "_installed_skill_names",
        lambda: {"oh-my-research", "deep-research", "autoresearch"},
    )

    cat = doctor_cmd._check_reference_skills()
    assert cat["status"] == "ok"
    assert all(c["status"] == "ok" for c in cat["checks"])


def test_check_anydoc_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import doctor_cmd

    monkeypatch.setattr(doctor_cmd, "_find_anydoc", lambda: None)

    cat = doctor_cmd._check_anydoc()
    assert cat["category"] == "anydoc"
    assert cat["status"] == "warning"
    assert cat["checks"][0]["status"] == "warning"
    assert "npm install -g @firecrawl/anydoc" in cat["checks"][0]["details"]["remediation"]


def test_check_anydoc_present(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import doctor_cmd

    monkeypatch.setattr(doctor_cmd, "_find_anydoc", lambda: "/usr/local/bin/anydoc")
    monkeypatch.setattr(doctor_cmd, "_bin_version", lambda _path: None)

    cat = doctor_cmd._check_anydoc()
    assert cat["status"] == "ok"
    assert cat["checks"][0]["status"] == "ok"


def test_run_doctor_includes_browser_category(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import doctor_cmd

    async def fake_diagnose(_config=None, **_kwargs):
        return []

    monkeypatch.setattr("flowjet.cli.doctor_cmd._run_diagnose", fake_diagnose)
    monkeypatch.setattr(
        "flowjet.cli.doctor_cmd._check_browser_deps",
        lambda: {
            "category": "browser",
            "status": "ok",
            "checks": [{"name": "chrome", "status": "ok", "message": "chrome ok", "details": {}}],
            "message": None,
        },
    )
    monkeypatch.setattr(
        "flowjet.cli.doctor_cmd._check_reference_skills", lambda **_k: _stub_reference_skills_ok()
    )
    monkeypatch.setattr("flowjet.cli.doctor_cmd._check_anydoc", lambda: _stub_anydoc_ok())
    monkeypatch.setattr(
        "flowjet.cli.agent.load_config",
        lambda _path=None: SimpleNamespace(),
    )

    assert doctor_cmd.run_doctor(["--format", "json", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert '"category": "browser"' in out
