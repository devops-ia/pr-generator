"""Tests for logging setup."""

import json
import logging

from pr_generator.logging_config import setup_logging


class TestSetupLogging:
    def test_text_format_sets_level(self):
        setup_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_info_level(self):
        setup_logging("INFO")
        assert logging.getLogger().level == logging.INFO

    def test_invalid_level_falls_back_to_info(self):
        setup_logging("NOTAREAL")
        assert logging.getLogger().level == logging.INFO

    def test_text_format_is_plain_formatter(self):
        setup_logging("INFO", json_format=False)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert not isinstance(root.handlers[0].formatter, logging.Formatter.__class__)

    def test_json_format_emits_valid_json(self):
        setup_logging("INFO", json_format=True)
        root = logging.getLogger()
        formatter = root.handlers[0].formatter
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello %s", args=("world",), exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "hello world"
        assert parsed["level"] == "INFO"
        assert "timestamp" in parsed

    def test_json_format_includes_exception(self):
        setup_logging("INFO", json_format=True)
        formatter = logging.getLogger().handlers[0].formatter
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="err", args=(), exc_info=exc_info,
        )
        output = json.loads(formatter.format(record))
        assert "exception" in output
        assert "ValueError" in output["exception"]

    def test_replaces_existing_handlers(self):
        root = logging.getLogger()
        root.addHandler(logging.NullHandler())
        initial_count = len(root.handlers)
        setup_logging("INFO")
        assert len(root.handlers) == 1

    def test_json_format_includes_stack_info(self):
        setup_logging("INFO", json_format=True)
        formatter = logging.getLogger().handlers[0].formatter
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="with stack", args=(), exc_info=None,
        )
        record.stack_info = "Stack (most recent call last):\n  File 'x.py', line 1"
        output = json.loads(formatter.format(record))
        assert "stack_info" in output
        assert "most recent" in output["stack_info"]
