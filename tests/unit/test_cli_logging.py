"""Levelling through the vedit logger (VEDIT_LOG_LEVEL drives CLI output)."""

from __future__ import annotations

import logging

from vedit import cli


def test_log_emits_info(caplog):
    with caplog.at_level(logging.INFO, logger="vedit"):
        cli._log("[stage] progress")
    assert "[stage] progress" in caplog.text
    assert caplog.records[-1].levelname == "INFO"


def test_warn_emits_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="vedit"):
        cli._warn("[qc] FAIL duration: drift")
    assert "[qc] FAIL duration" in caplog.text
    assert caplog.records[-1].levelname == "WARNING"


def test_setup_logging_honours_env_level(monkeypatch, caplog):
    monkeypatch.setenv("VEDIT_LOG_LEVEL", "WARNING")
    previous = cli._LOG.level
    try:
        cli._setup_logging()
        assert cli._LOG.level == logging.WARNING
        with caplog.at_level(logging.WARNING, logger="vedit"):
            cli._log("hidden info")
            cli._warn("shown warning")
    finally:
        cli._LOG.setLevel(previous)
    assert "hidden info" not in caplog.text
    assert "shown warning" in caplog.text
