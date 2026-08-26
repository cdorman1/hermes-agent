"""Telegram integration for the KMFS coach on-shift checklist."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COACH_CHECKLIST_MEMBER_MENU_COMMANDS = [
    ("coachchecklist", "Start coach checklist"),
]
COACH_CHECKLIST_ADMIN_MENU_COMMANDS = [
    ("coachchecklist", "Start coach checklist"),
    ("coachchecklists", "View coach checklist submissions"),
    ("coachchecklistexport", "Export coach checklist CSV"),
]
COACH_CHECKLIST_ADMIN_ONLY_COMMANDS = {
    "/coachchecklists",
    "/coachchecklistexport",
}
TELEGRAM_ADMIN_STATUSES = {"administrator", "creator"}
_INLINE_RETRY_CAP_SECONDS = 5.0


def coach_checklist_admin_menu_commands(
    base_commands: list[tuple[str, str]],
    *,
    max_commands: int,
) -> list[tuple[str, str]]:
    """Pin checklist administration commands ahead of the normal menu."""
    pinned_names = {name for name, _desc in COACH_CHECKLIST_ADMIN_MENU_COMMANDS}
    merged = list(COACH_CHECKLIST_ADMIN_MENU_COMMANDS)
    merged.extend(
        (name, desc) for name, desc in base_commands if name not in pinned_names
    )
    return merged[:max_commands]


def coach_checklist_member_menu_commands(
    base_commands: list[tuple[str, str]],
    *,
    max_commands: int,
) -> list[tuple[str, str]]:
    """Pin the checklist start command without hiding normal Hermes commands."""
    merged = list(COACH_CHECKLIST_MEMBER_MENU_COMMANDS)
    merged.extend(
        (name, desc)
        for name, desc in base_commands
        if name != "coachchecklist"
    )
    return merged[:max_commands]


class CoachChecklistTelegramMixin:
    """Checklist handlers mixed into the Telegram adapter."""

    def _coach_checklist_allowed_chat_ids(self) -> set[str]:
        from gateway import coach_checklist as cc

        return cc.configured_allowed_chat_ids(self.config.extra)

    def _coach_checklist_enabled(self) -> bool:
        return bool(self._coach_checklist_allowed_chat_ids())

    async def _safe_coach_checklist_edit(
        self,
        query: Any,
        text: str,
        *,
        reply_markup: Any = None,
        log_label: str = "message edit",
    ) -> bool:
        """Edit without turning a long Telegram throttle into a gateway stall."""
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
            return True
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                wait_seconds = max(float(retry_after) + 1.0, 1.0)
                if wait_seconds > _INLINE_RETRY_CAP_SECONDS:
                    logger.warning(
                        "[%s] Coach checklist %s throttle %.1fs exceeds inline cap",
                        self.name,
                        log_label,
                        wait_seconds,
                    )
                    return False
                logger.warning(
                    "[%s] Coach checklist %s throttled; retrying in %.1fs",
                    self.name,
                    log_label,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                try:
                    await query.edit_message_text(text, reply_markup=reply_markup)
                    return True
                except Exception as retry_exc:
                    logger.warning(
                        "[%s] Coach checklist %s retry failed: %s",
                        self.name,
                        log_label,
                        retry_exc,
                    )
                    return False
            if "Message is not modified" in str(exc):
                return True
            logger.warning(
                "[%s] Coach checklist %s failed: %s",
                self.name,
                log_label,
                exc,
            )
            return False

    def _coach_checklist_admin_user_ids(self) -> set[str]:
        raw_values: list[str] = []
        raw_cc = self.config.extra.get("coach_checklist")
        cc_config = raw_cc if isinstance(raw_cc, dict) else {}
        for value in (
            cc_config.get("admin_user_ids"),
            self.config.extra.get("coach_checklist_admin_user_ids"),
        ):
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                raw_values.extend(str(item) for item in value)
            else:
                raw_values.extend(str(value).split(","))
        return {value.strip() for value in raw_values if value.strip()}

    async def _is_coach_checklist_admin(
        self,
        context: Any,
        chat_id: int,
        user_id: int,
    ) -> bool:
        if str(user_id) in self._coach_checklist_admin_user_ids():
            return True
        try:
            member = await context.bot.get_chat_member(
                chat_id=chat_id,
                user_id=user_id,
            )
        except Exception as exc:
            logger.warning(
                "[%s] Could not verify Telegram checklist admin for chat=%s user=%s: %s",
                self.name,
                chat_id,
                user_id,
                exc,
            )
            return False
        return str(getattr(member, "status", "")).lower() in TELEGRAM_ADMIN_STATUSES

    async def _handle_coach_checklist_command(self, update: Any, context: Any) -> bool:
        msg = self._effective_update_message(update)
        if not msg or not msg.text:
            return False
        command = (msg.text.split(maxsplit=1)[0] or "").split("@", 1)[0].lower()
        if command not in {
            "/coachchecklist",
            "/coachchecklists",
            "/coachchecklistexport",
        }:
            return False

        from gateway import coach_checklist as cc
        from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

        chat_id = getattr(msg, "chat_id", None)
        thread_id = getattr(msg, "message_thread_id", None)
        if chat_id is None:
            await msg.reply_text("Coach checklist cannot determine this chat ID.")
            return True
        if not cc.chat_allowed(chat_id, self.config.extra):
            logger.warning(
                "[%s] Unauthorized coach checklist command in chat=%s",
                self.name,
                chat_id,
            )
            await msg.reply_text(
                "⛔ Coach checklist is not authorized in this Telegram chat."
            )
            return True

        if command in COACH_CHECKLIST_ADMIN_ONLY_COMMANDS:
            user_id = getattr(getattr(msg, "from_user", None), "id", None)
            if user_id is None or not await self._is_coach_checklist_admin(
                context,
                int(chat_id),
                int(user_id),
            ):
                await msg.reply_text(
                    "⛔ Only Telegram chat admins or configured checklist admins "
                    "can view or export coach checklist submissions."
                )
                return True

        if command == "/coachchecklist":
            user = getattr(msg, "from_user", None)
            if user is None:
                await msg.reply_text(
                    "Coach checklist requires a Telegram user identity."
                )
                return True
            session = cc.create_session(
                user,
                int(chat_id),
                thread_id=int(thread_id) if thread_id is not None else None,
                message_id=getattr(msg, "message_id", None),
            )
            await msg.reply_text(
                cc.render_trial_prompt(session),
                reply_markup=ForceReply(
                    selective=True,
                    input_field_placeholder="Enter trial/walk-in info or skip",
                ),
            )
            return True

        if command == "/coachchecklists":
            parts = msg.text.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else None
            try:
                text = await asyncio.to_thread(cc.recent_submissions_text, arg)
            except Exception as exc:
                logger.error(
                    "[%s] Coach checklist query failed: %s",
                    self.name,
                    exc,
                    exc_info=True,
                )
                text = "Coach checklist query failed. Check gateway logs."
            await msg.reply_text(text)
            return True

        export_path: Path | None = None
        try:
            export_path = await asyncio.to_thread(cc.export_csv)
            with export_path.open("rb") as handle:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=handle,
                    filename=export_path.name,
                    caption="Coach checklist CSV export",
                    **self._thread_kwargs_for_send(
                        str(chat_id),
                        str(thread_id) if thread_id is not None else None,
                        {"thread_id": str(thread_id)} if thread_id is not None else None,
                    ),
                )
        except Exception as exc:
            logger.error(
                "[%s] Coach checklist export failed: %s",
                self.name,
                exc,
                exc_info=True,
            )
            await msg.reply_text("Coach checklist export failed. Check gateway logs.")
        finally:
            if export_path is not None:
                export_path.unlink(missing_ok=True)
        return True

    async def _handle_coach_checklist_text(self, update: Any, context: Any) -> bool:
        msg = self._effective_update_message(update)
        user = getattr(msg, "from_user", None) if msg else None
        if not msg or not msg.text or user is None:
            return False

        from gateway import coach_checklist as cc
        from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

        session = cc.active_trial_session(
            getattr(msg, "chat_id", None),
            getattr(user, "id", None),
        )
        if session:
            try:
                session, result = cc.handle_trial_input(
                    session.session_id,
                    msg.text.strip(),
                )
                if result == "attendance" and session:
                    await msg.reply_text(
                        cc.render_trial_attendance_prompt(session),
                        reply_markup=InlineKeyboardMarkup(
                            cc.trial_attendance_keyboard(
                                session,
                                InlineKeyboardButton,
                            )
                        ),
                    )
                elif result == "complete" and session:
                    await msg.reply_text(
                        cc.render_section(session),
                        reply_markup=InlineKeyboardMarkup(
                            cc.section_keyboard(session, InlineKeyboardButton)
                        ),
                    )
                elif session:
                    await msg.reply_text(
                        cc.render_trial_prompt(session),
                        reply_markup=ForceReply(
                            selective=True,
                            input_field_placeholder="Enter trial/walk-in info or skip",
                        ),
                    )
            except Exception as exc:
                logger.error(
                    "[%s] Coach checklist trial intake failed: %s",
                    self.name,
                    exc,
                    exc_info=True,
                )
                await msg.reply_text(
                    "Coach checklist trial/walk-in intake failed. Please notify management."
                )
            return True

        session = cc.active_incident_session(
            getattr(msg, "chat_id", None),
            getattr(user, "id", None),
        )
        if not session:
            return False
        session.incident_notes = msg.text.strip()
        try:
            await asyncio.to_thread(cc.persist_submission, session)
            await msg.reply_text(cc.confirmation_text(session))
            alert_chat = cc.management_chat_id()
            if alert_chat:
                await context.bot.send_message(
                    chat_id=int(alert_chat),
                    text=cc.incident_alert_text(session),
                )
        except Exception as exc:
            logger.error(
                "[%s] Coach checklist incident submission failed: %s",
                self.name,
                exc,
                exc_info=True,
            )
            await msg.reply_text(
                "Coach checklist incident submission failed. Please notify management."
            )
        return True

    async def _handle_coach_checklist_callback(
        self,
        query: Any,
        data: str,
        context: Any = None,
    ) -> bool:
        if not data.startswith("cc:"):
            return False

        from gateway import coach_checklist as cc
        from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

        parts = data.split(":", 3)
        if len(parts) < 3:
            await query.answer(text="Invalid checklist action.")
            return True
        _, session_id, action = parts[:3]
        arg = parts[3] if len(parts) == 4 else None
        msg = getattr(query, "message", None)
        chat_id = getattr(msg, "chat_id", None)
        if not cc.chat_allowed(chat_id, self.config.extra):
            await query.answer(text="⛔ Not authorized in this chat.")
            return True

        existing = cc.get_session(session_id)
        caller_id = getattr(getattr(query, "from_user", None), "id", None)
        if existing is not None and str(existing.telegram_user_id) != str(caller_id):
            await query.answer(text="⛔ This checklist belongs to another coach.")
            return True

        if action == "trial" and arg is not None:
            session, result = cc.handle_trial_attendance(session_id, arg)
            if result == "expired" or not session:
                await query.answer(text="This checklist has expired. Start a new one.")
                return True
            if result != "more":
                await query.answer(text="Invalid trial status.")
                return True
            await query.answer(text="Trial status saved")
            await self._safe_coach_checklist_edit(
                query,
                cc.render_trial_more_prompt(session),
                reply_markup=InlineKeyboardMarkup(
                    cc.trial_more_keyboard(session, InlineKeyboardButton)
                ),
                log_label="trial-more edit",
            )
            return True

        if action in {"trialadd", "trialdone"}:
            session, result = cc.handle_trial_more_action(session_id, action)
            if result == "expired" or not session:
                await query.answer(text="This checklist has expired. Start a new one.")
                return True
            if result == "add":
                await query.answer(text="Add another trial/walk-in")
                await self._safe_coach_checklist_edit(
                    query,
                    cc.render_trial_more_prompt(session),
                    reply_markup=None,
                    log_label="trial-add edit",
                )
                thread_id = getattr(msg, "message_thread_id", None)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=cc.render_trial_prompt(session),
                    reply_markup=ForceReply(
                        selective=True,
                        input_field_placeholder="Enter trial/walk-in info",
                    ),
                    **self._thread_kwargs_for_send(
                        str(chat_id),
                        str(thread_id) if thread_id is not None else None,
                        {"thread_id": str(thread_id)} if thread_id is not None else None,
                    ),
                )
                return True
            if result == "complete":
                await query.answer(text="Starting shift checklist")
                await self._safe_coach_checklist_edit(
                    query,
                    cc.render_section(session),
                    reply_markup=InlineKeyboardMarkup(
                        cc.section_keyboard(session, InlineKeyboardButton)
                    ),
                    log_label="section edit",
                )
                return True
            await query.answer(text="Trial/walk-in intake is not active.")
            return True

        if action == "trialskip":
            session, result = cc.skip_trial_intake(session_id)
            if result == "expired" or not session:
                await query.answer(text="This checklist has expired. Start a new one.")
                return True
            if result != "complete":
                await query.answer(text="Trial/walk-in intake is not active.")
                return True
            await query.answer(text="Trial/walk-in skipped")
            await self._safe_coach_checklist_edit(
                query,
                cc.render_section(session),
                reply_markup=InlineKeyboardMarkup(
                    cc.section_keyboard(session, InlineKeyboardButton)
                ),
                log_label="trial-skip section edit",
            )
            return True

        session, result = cc.handle_action(session_id, action, arg)
        if result == "expired" or not session:
            await query.answer(text="This checklist has expired. Start a new one.")
            await self._safe_coach_checklist_edit(
                query,
                "Checklist expired. Start a new one with /coachchecklist.",
                reply_markup=None,
                log_label="expired edit",
            )
            return True
        if result == "already_submitted":
            await query.answer(text="Already submitted.")
            return True
        if action == "cancel":
            cc.remove_session(session_id)
            await query.answer(text="Cancelled")
            await self._safe_coach_checklist_edit(
                query,
                "Coach checklist cancelled.",
                reply_markup=None,
                log_label="cancel edit",
            )
            return True
        if action == "submit":
            try:
                await asyncio.to_thread(cc.persist_submission, session)
                await query.answer(text="Submitted")
                await self._safe_coach_checklist_edit(
                    query,
                    cc.confirmation_text(session),
                    reply_markup=None,
                    log_label="submit confirmation edit",
                )
            except Exception as exc:
                logger.error(
                    "[%s] Coach checklist submit failed: %s",
                    self.name,
                    exc,
                    exc_info=True,
                )
                await query.answer(text="Submit failed.")
            return True
        if action == "incident":
            await query.answer(text="Type the incident description.")
            await self._safe_coach_checklist_edit(
                query,
                cc.incident_prompt(),
                reply_markup=None,
                log_label="incident prompt edit",
            )
            return True

        if session.status == "review":
            text = cc.render_review(session)
            markup = InlineKeyboardMarkup(
                cc.review_keyboard(session, InlineKeyboardButton)
            )
        else:
            text = cc.render_section(session)
            markup = InlineKeyboardMarkup(
                cc.section_keyboard(session, InlineKeyboardButton)
            )
        edit_ok = await self._safe_coach_checklist_edit(
            query,
            text,
            reply_markup=markup,
            log_label="checklist edit",
        )
        if edit_ok:
            await query.answer()
        else:
            await query.answer(text="Checklist saved; message edit failed.")
        return True
