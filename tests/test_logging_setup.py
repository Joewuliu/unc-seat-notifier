from __future__ import annotations

import json
import logging

from unc_notifier.logging_setup import configure_logging


def test_configure_logging_writes_json_lines(tmp_path):
    log_file = tmp_path / "app.log"
    configure_logging(log_file)

    logger = logging.getLogger("unc_notifier.test")
    logger.info("observed section=%s seats=%d", "COMP550-001", 3, extra={"course": "COMP550"})

    for handler in logging.getLogger().handlers:
        handler.flush()

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["level"] == "INFO"
    assert record["logger"] == "unc_notifier.test"
    assert "COMP550-001" in record["message"]
    assert record["extra"]["course"] == "COMP550"


def test_configure_logging_is_idempotent(tmp_path):
    log_file = tmp_path / "app.log"
    configure_logging(log_file)
    configure_logging(log_file)

    logger = logging.getLogger("unc_notifier.test_idempotent")
    logger.info("one line only")

    for handler in logging.getLogger().handlers:
        handler.flush()

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
