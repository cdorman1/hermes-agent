import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from cron.scheduler import (
    _deliver_result,
    _flush_quiet_hours_queue,
    _is_p0_critical_exception,
    _is_telegram_quiet_hours,
    _load_quiet_hours_queue,
    _next_quiet_hours_release,
    _quiet_hours_queue_path,
    _should_queue_telegram_delivery,
)


def _telegram_config():
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: pconfig}
    return config


def test_quiet_hours_window_and_p0_exception():
    quiet = datetime(2026, 6, 10, 5, 30, tzinfo=timezone.utc)
    daytime = datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc)

    assert _is_telegram_quiet_hours(quiet) is True
    assert _is_telegram_quiet_hours(daytime) is False
    assert _next_quiet_hours_release(quiet).hour == 7
    assert _should_queue_telegram_delivery(
        {"platform": "telegram"},
        "routine",
        quiet,
    )
    assert not _should_queue_telegram_delivery(
        {"platform": "discord"},
        "routine",
        quiet,
    )
    assert _is_p0_critical_exception(
        "P0 Critical system outage cannot wait"
    )
    assert not _is_p0_critical_exception("critical but routine summary")


def test_routine_telegram_delivery_is_queued(tmp_path):
    quiet = datetime(2026, 6, 10, 5, 30, tzinfo=timezone.utc)
    send = AsyncMock(return_value={"success": True})
    job = {
        "id": "quiet-job",
        "name": "status-watchdog",
        "deliver": "origin",
        "origin": {
            "platform": "telegram",
            "chat_id": "123",
            "thread_id": "456",
        },
    }

    with (
        patch("gateway.config.load_gateway_config", return_value=_telegram_config()),
        patch("tools.send_message_tool._send_to_platform", new=send),
        patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
        patch("cron.scheduler._quiet_hours_now", return_value=quiet),
    ):
        error = _deliver_result(job, "Routine status: all monitors green.")
        entries = _load_quiet_hours_queue()
        queue_mode = _quiet_hours_queue_path().stat().st_mode & 0o777

    assert error is None
    send.assert_not_called()
    assert queue_mode == 0o600
    assert len(entries) == 1
    assert entries[0]["job_id"] == "quiet-job"
    assert entries[0]["target"] == {
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "456",
    }


def test_p0_telegram_delivery_bypasses_queue(tmp_path):
    quiet = datetime(2026, 6, 10, 5, 30, tzinfo=timezone.utc)
    send = AsyncMock(return_value={"success": True})
    job = {
        "id": "p0-job",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }

    with (
        patch("gateway.config.load_gateway_config", return_value=_telegram_config()),
        patch("tools.send_message_tool._send_to_platform", new=send),
        patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
        patch("cron.scheduler._quiet_hours_now", return_value=quiet),
    ):
        error = _deliver_result(
            job,
            "P0 Critical system outage cannot wait; human action needed now.",
        )

    assert error is None
    send.assert_called_once()
    assert not (tmp_path / "cron" / "signal_governor_telegram_delivery_queue.jsonl").exists()


def test_released_queue_entry_is_sent_and_removed(tmp_path):
    queue_path = tmp_path / "cron" / "signal_governor_telegram_delivery_queue.jsonl"
    queue_path.parent.mkdir(parents=True)
    queue_path.write_text(
        json.dumps(
            {
                "created_at": "2026-06-10T05:30:00+00:00",
                "release_after": "2000-01-01T12:00:00+00:00",
                "job_id": "queued-job",
                "job_name": "daily-brief",
                "target": {
                    "platform": "telegram",
                    "chat_id": "123",
                    "thread_id": None,
                },
                "content": "Deferred daily brief.",
                "media_files": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    send = AsyncMock(return_value={"success": True})

    with (
        patch("gateway.config.load_gateway_config", return_value=_telegram_config()),
        patch("tools.send_message_tool._send_to_platform", new=send),
        patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
        patch("cron.scheduler._is_telegram_quiet_hours", return_value=False),
    ):
        delivered = _flush_quiet_hours_queue()

    assert delivered == 1
    send.assert_called_once()
    assert not queue_path.exists()
