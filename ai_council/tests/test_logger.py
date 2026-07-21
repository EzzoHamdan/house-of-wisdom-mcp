"""Logger injectability (E9) and JSON format (E10)."""

from __future__ import annotations

import json
import logging

from ai_council.logger import AICouncilLogger, _JsonFormatter, _TextFormatter, _make_formatter


def test_instances_are_independent_not_a_singleton():
    a = AICouncilLogger()
    b = AICouncilLogger()
    assert a is not b  # no forced singleton; both usable


def test_shared_named_logger_has_one_handler():
    # Multiple instances must not stack duplicate handlers on the named logger.
    AICouncilLogger()
    AICouncilLogger()
    assert len(logging.getLogger("ai_council").handlers) == 1


def test_make_formatter_selects_type():
    assert isinstance(_make_formatter("json"), _JsonFormatter)
    assert isinstance(_make_formatter("text"), _TextFormatter)
    assert isinstance(_make_formatter("anything-else"), _TextFormatter)  # default


def test_json_formatter_emits_valid_line_with_data():
    fmt = _make_formatter("json")
    record = logging.LogRecord(
        name="ai_council", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    record.acdata = {"models": 3, "ok": True}
    line = fmt.format(record)
    obj = json.loads(line)  # must be a single valid JSON object
    assert obj["message"] == "hello"
    assert obj["level"] == "INFO"
    assert obj["data"] == {"models": 3, "ok": True}


def test_json_formatter_omits_data_when_none():
    fmt = _make_formatter("json")
    record = logging.LogRecord(
        name="ai_council", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="warn", args=(), exc_info=None,
    )
    record.acdata = None
    obj = json.loads(fmt.format(record))
    assert "data" not in obj


def test_set_format_swaps_handler_formatter():
    log = AICouncilLogger()
    log.set_format("json")
    handler = logging.getLogger("ai_council").handlers[0]
    assert isinstance(handler.formatter, _JsonFormatter)
    log.set_format("text")  # restore so other tests see text output
    assert isinstance(logging.getLogger("ai_council").handlers[0].formatter, _TextFormatter)
