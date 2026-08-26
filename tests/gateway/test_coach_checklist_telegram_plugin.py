from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import coach_checklist as cc
from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.coach_checklist import (
    coach_checklist_admin_menu_commands,
    coach_checklist_member_menu_commands,
)


def _adapter() -> TelegramAdapter:
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="test", extra={})
    return adapter


def test_checklist_commands_are_pinned_without_hiding_normal_commands():
    base = [(f"cmd{index}", f"Command {index}") for index in range(10)]

    member = coach_checklist_member_menu_commands(base, max_commands=10)
    admin = coach_checklist_admin_menu_commands(base, max_commands=10)

    assert [name for name, _desc in member][:2] == ["coachchecklist", "cmd0"]
    assert [name for name, _desc in admin][:3] == [
        "coachchecklist",
        "coachchecklists",
        "coachchecklistexport",
    ]
    assert len(member) == 10
    assert len(admin) == 10


@pytest.mark.asyncio
async def test_callback_cannot_modify_another_coachs_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("COACH_CHECKLIST_ALLOWED_CHAT_IDS", "-1001")
    owner = SimpleNamespace(
        id=123,
        username="owner",
        first_name="Owner",
        last_name="Coach",
    )
    session = cc.create_session(owner, -1001)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=456),
        message=SimpleNamespace(chat_id=-1001, message_thread_id=None),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    consumed = await _adapter()._handle_coach_checklist_callback(
        query,
        f"cc:{session.session_id}:trialskip",
        SimpleNamespace(bot=AsyncMock()),
    )

    assert consumed is True
    query.answer.assert_awaited_once_with(
        text="⛔ This checklist belongs to another coach."
    )
    query.edit_message_text.assert_not_awaited()
    assert cc.get_session(session.session_id).status == "awaiting_trial"
