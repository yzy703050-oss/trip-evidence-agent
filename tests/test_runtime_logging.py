import importlib
import logging
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def log_file(tmp_path):
    module = importlib.import_module("trip_assistant.runtime.logging")
    path = module.configure_runtime_logging(tmp_path)
    yield path
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if getattr(handler, "_travel_runtime", False):
            root.removeHandler(handler)
            handler.close()


def test_utf8_shanghai_idempotence_and_external_handler_preserved(log_file):
    from trip_assistant.runtime.logging import configure_runtime_logging

    external = logging.NullHandler()
    logging.getLogger().addHandler(external)
    try:
        assert configure_runtime_logging(log_file.parent) == log_file
        logging.getLogger("agents.test").info("中文普通运行")
        text = log_file.read_text(encoding="utf-8")
        assert text.count("中文普通运行") == 1
        assert datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d") in text
        configure_runtime_logging(log_file.parent / "other")
        assert external in logging.getLogger().handlers
    finally:
        logging.getLogger().removeHandler(external)


def test_real_rotation_is_bounded(log_file):
    from trip_assistant.runtime.logging import configure_runtime_logging

    configure_runtime_logging(log_file.parent, max_bytes=250, backup_count=2)
    for i in range(30):
        logging.getLogger("agents.test").info("轮转%s%s", i, "x" * 100)
    assert (log_file.parent / "runtime.log.1").exists()
    assert (log_file.parent / "runtime.log.2").exists()
    assert not (log_file.parent / "runtime.log.3").exists()


@pytest.mark.asyncio
async def test_model_usage_survives_database_failure_without_payload(log_file):
    from trip_assistant.runtime.telemetry import MeteredModel, model_stage

    async def model(messages):
        return SimpleNamespace(
            content="PRIVATE_MODEL", usage=SimpleNamespace(input_tokens=123, output_tokens=45)
        )

    def fail(event):
        raise OSError("database unavailable")

    with model_stage("main:summary"):
        await MeteredModel(model, fail)([{"role": "user", "content": "PRIVATE_USER"}])
    text = log_file.read_text(encoding="utf-8")
    assert "api_input_tokens=123" in text and "api_output_tokens=45" in text
    assert "main:summary" in text and "PRIVATE_" not in text


@pytest.mark.asyncio
async def test_unknown_usage_and_error_propagation(log_file):
    from trip_assistant.runtime.telemetry import MeteredModel

    async def no_usage(messages):
        return SimpleNamespace(content="ok", usage=None)

    async def fail(messages):
        raise ValueError("failed model")

    await MeteredModel(no_usage, lambda e: None)([])
    with pytest.raises(ValueError):
        await MeteredModel(fail, lambda e: None)([])
    text = log_file.read_text(encoding="utf-8")
    assert "api_input_tokens=unknown" in text and "status=error" in text


def test_budget_diagnostic_separates_soft_trigger_and_hard_budget(log_file):
    from trip_assistant.llm.budget import BudgetedModel

    BudgetedModel(None, budget_tokens=20000).check([{"role": "user", "content": "PRIVATE_PROMPT"}])
    text = log_file.read_text(encoding="utf-8")
    assert "budget_tokens=20000" in text and "trigger_tokens=16000" in text
    assert "estimated=True" in text and "PRIVATE_PROMPT" not in text


@pytest.mark.parametrize("health", [True, False])
def test_cli_entry_configures_log_before_health_or_interactive(monkeypatch, log_file, health):
    import trip_assistant.cli as cli

    called = []
    monkeypatch.setattr(
        cli, "configure_runtime_logging", lambda **kw: called.append("logging") or log_file
    )
    monkeypatch.setattr(cli.sys, "argv", ["cli.py", "health"] if health else ["cli.py"])

    async def health_check(settings):
        called.append("health")
        return True

    monkeypatch.setattr(cli, "health", health_check)

    async def run():
        called.append("interactive")

    monkeypatch.setattr(cli, "TripEvidenceCLI", lambda: SimpleNamespace(run=run))
    if health:
        with pytest.raises(SystemExit):
            cli.main()
    else:
        cli.main()
    assert called == ["logging", "health" if health else "interactive"]


def test_importing_cli_does_not_create_log(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    project = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {project!r}); import cli"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "data/logs").exists()
