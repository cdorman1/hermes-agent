"""Tests for lazy forum command registration in TelegramAdapter."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


def _make_test_adapter():
    """Build a TelegramAdapter without running __init__."""
    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={})
    # ``name`` is a property derived from platform.value.title()
    adapter._bot = MagicMock()
    adapter._bot.set_my_commands = AsyncMock()
    adapter._forum_command_registered = set()
    adapter._forum_lock = asyncio.Lock()
    return adapter


def _forum_message(chat_id=-100, is_forum=True):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, is_forum=is_forum),
    )


@pytest.mark.asyncio
async def test_ensure_forum_commands_skips_non_forum():
    adapter = _make_test_adapter()
    msg = _forum_message(is_forum=False)
    await adapter._ensure_forum_commands(msg)
    adapter._bot.set_my_commands.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_forum_commands_skips_already_registered():
    adapter = _make_test_adapter()
    adapter._forum_command_registered.add(-100)
    msg = _forum_message(is_forum=True)
    await adapter._ensure_forum_commands(msg)
    adapter._bot.set_my_commands.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_forum_commands_registers_once(monkeypatch):
    monkeypatch.delenv("COACH_CHECKLIST_ALLOWED_CHAT_IDS", raising=False)
    adapter = _make_test_adapter()
    msg = _forum_message(chat_id=-123, is_forum=True)

    with patch("hermes_cli.commands.telegram_menu_commands") as mock_menu:
        mock_menu.return_value = ([("new", "Start new session"), ("help", "Show help")], 0)
        with patch("telegram.BotCommand") as MockBotCommand:
            instances = []

            def _make_cmd(name, desc):
                cmd = MagicMock()
                cmd.name = name
                cmd.description = desc
                instances.append(cmd)
                return cmd

            MockBotCommand.side_effect = _make_cmd
            with patch("telegram.BotCommandScopeChat") as MockScope:
                # Track the chat_id passed to the BotCommandScopeChat constructor
                # so the assertions below see an int instead of a bare MagicMock.
                def _make_scope(chat_id):
                    s = MagicMock()
                    s.chat_id = chat_id
                    return s
                MockScope.side_effect = _make_scope
                await adapter._ensure_forum_commands(msg)

    assert -123 in adapter._forum_command_registered
    adapter._bot.set_my_commands.assert_awaited_once()
    args, kwargs = adapter._bot.set_my_commands.call_args
    assert len(args[0]) == 2  # two BotCommand instances
    assert kwargs["scope"] is not None
    assert isinstance(kwargs["scope"].chat_id, int)
    assert kwargs["scope"].chat_id == -123


@pytest.mark.asyncio
async def test_ensure_forum_commands_handles_set_failure():
    adapter = _make_test_adapter()
    msg = _forum_message(chat_id=-456, is_forum=True)
    adapter._bot.set_my_commands.side_effect = Exception("Telegram API error")

    with patch("hermes_cli.commands.telegram_menu_commands") as mock_menu:
        mock_menu.return_value = ([("new", "Start new session")], 0)
        # Should NOT raise despite the API error
        await adapter._ensure_forum_commands(msg)

    # On failure we don't retry for this chat, so it's added to the set
    # to avoid hammering a broken chat.
    assert -456 not in adapter._forum_command_registered


@pytest.mark.asyncio
async def test_ensure_forum_commands_race_safety():
    """Two concurrent coroutines must not double-register the same chat."""
    adapter = _make_test_adapter()
    msg = _forum_message(chat_id=-789, is_forum=True)

    with patch("hermes_cli.commands.telegram_menu_commands") as mock_menu:
        mock_menu.return_value = ([("new", "Start new session")], 0)
        with patch("telegram.BotCommand"):
            with patch("telegram.BotCommandScopeChat"):
                coro1 = adapter._ensure_forum_commands(msg)
                coro2 = adapter._ensure_forum_commands(msg)
                await asyncio.gather(coro1, coro2)

    # The lock should make this exactly 1 call, not 2.
    assert adapter._bot.set_my_commands.await_count == 1


def test_coach_checklist_admin_menu_commands_are_pinned_first():
    """Coach checklist admin commands must appear even when base menu is full."""
    from gateway.platforms.telegram import _coach_checklist_admin_menu_commands

    full_base_menu = [(f"cmd{i}", f"Command {i}") for i in range(30)]

    commands = _coach_checklist_admin_menu_commands(full_base_menu, max_commands=30)

    names = [name for name, _desc in commands]
    assert names[:3] == ["coachchecklist", "coachchecklists", "coachchecklistexport"]
    assert len(commands) == 30


@pytest.mark.asyncio
async def test_coach_checklist_query_requires_chat_admin(monkeypatch):
    """Viewing submitted checklist data must be admin-only."""
    from gateway import coach_checklist as cc

    adapter = _make_test_adapter()
    msg = SimpleNamespace(
        text="/coachchecklists",
        chat_id=-1003947231173,
        message_thread_id=None,
        from_user=SimpleNamespace(id=7169074832),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_message=msg, message=msg)
    context = SimpleNamespace(bot=AsyncMock())
    context.bot.get_chat_member.return_value = SimpleNamespace(status="member")

    called = False

    def _recent(_arg=None):
        nonlocal called
        called = True
        return "should not be returned"

    monkeypatch.setattr(cc, "chat_allowed", lambda chat_id, extra=None: True)
    monkeypatch.setattr(cc, "recent_submissions_text", _recent)

    consumed = await adapter._handle_coach_checklist_command(update, context)

    assert consumed is True
    assert called is False
    msg.reply_text.assert_awaited_once()
    assert "admin" in msg.reply_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_coach_checklist_query_allows_chat_admin(monkeypatch):
    """Admins can view submitted checklist data."""
    from gateway import coach_checklist as cc

    adapter = _make_test_adapter()
    msg = SimpleNamespace(
        text="/coachchecklists today",
        chat_id=-1003947231173,
        message_thread_id=None,
        from_user=SimpleNamespace(id=7169074832),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_message=msg, message=msg)
    context = SimpleNamespace(bot=AsyncMock())
    context.bot.get_chat_member.return_value = SimpleNamespace(status="administrator")

    seen_args = []

    def _recent(arg=None):
        seen_args.append(arg)
        return "recent checklist data"

    monkeypatch.setattr(cc, "chat_allowed", lambda chat_id, extra=None: True)
    monkeypatch.setattr(cc, "recent_submissions_text", _recent)

    consumed = await adapter._handle_coach_checklist_command(update, context)

    assert consumed is True
    assert seen_args == ["today"]
    msg.reply_text.assert_awaited_once_with("recent checklist data")


@pytest.mark.asyncio
async def test_coach_checklist_trialadd_callback_sends_force_reply(monkeypatch):
    """The trial-add callback must use the Telegram context bot, not an undefined local."""
    from gateway import coach_checklist as cc

    adapter = _make_test_adapter()
    adapter._thread_kwargs_for_send = lambda *args, **kwargs: {}

    session = SimpleNamespace(session_id="sess1")
    monkeypatch.setattr(cc, "chat_allowed", lambda chat_id, extra=None: True)
    monkeypatch.setattr(cc, "handle_trial_more_action", lambda session_id, action: (session, "add"))
    monkeypatch.setattr(cc, "render_trial_more_prompt", lambda session: "more prompt")
    monkeypatch.setattr(cc, "render_trial_prompt", lambda session: "trial prompt")

    query = SimpleNamespace(
        data="cc:sess1:trialadd",
        message=SimpleNamespace(chat_id=-1003947231173, message_thread_id=None),
        from_user=SimpleNamespace(first_name="Coach"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(bot=AsyncMock())
    update = SimpleNamespace(callback_query=query)

    await adapter._handle_callback_query(update, context)

    query.answer.assert_awaited_once_with(text="Add another trial/walk-in")
    query.edit_message_text.assert_awaited_once_with("more prompt", reply_markup=None)
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.await_args.kwargs["chat_id"] == -1003947231173
    assert context.bot.send_message.await_args.kwargs["text"] == "trial prompt"
