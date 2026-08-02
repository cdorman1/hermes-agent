"""Krav Maga Force Coach On Shift Checklist workflow for Telegram.

This module intentionally keeps the interactive workflow outside the LLM/agent
path.  It reuses the existing Telegram adapter, bot token, chat/topic routing,
and local PostgreSQL server.  Active sessions are persisted as JSON under
Hermes state so a gateway restart does not turn button presses into corrupt or
partial submissions; completed submissions are stored in PostgreSQL.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import secrets
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
SESSION_TTL = timedelta(hours=4)
CALLBACK_PREFIX = "cc"
DEFAULT_DB_NAME = "postgres"

CHECKLIST_SECTIONS: List[Tuple[str, List[str]]] = [
    (
        "Student and Floor Presence",
        [
            "Greet arriving students",
            "Talk with students between classes",
            "Learn and use student names",
            "Check in with new or trial students",
            "Watch students drill and correct technique",
            "Monitor safety and training intensity",
        ],
    ),
    (
        "Facility and Equipment",
        [
            "Sanitize mats",
            "Wipe down pads and shields",
            "Organize equipment",
            "Inspect equipment for damage",
            "Restock wipes and sanitizer",
            "Pick up trash and clutter",
            "Check bathrooms",
            "Check lobby",
        ],
    ),
    (
        "Class Support",
        [
            "Prepare pads or training stations",
            "Review the upcoming class plan",
            "Assist another coach",
            "Help students who are struggling",
            "Support testing or evaluations",
            "Mentor junior students",
        ],
    ),
    (
        "Gym Culture and Supervision",
        [
            "Welcome new members",
            "Introduce students to other members",
            "Reinforce gym rules respectfully",
            "Address unsafe behavior early",
            "Supervise children and teens",
            "Maintain a professional presence",
        ],
    ),
    (
        "Coach Development",
        [
            "Drill techniques",
            "Review curriculum",
            "Practice coaching cues",
            "Shadow a senior instructor",
            "Cross train in another program",
        ],
    ),
]

TOTAL_TASKS = sum(len(tasks) for _, tasks in CHECKLIST_SECTIONS)

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS coach_shift_checklist_submissions (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    telegram_username TEXT,
    coach_name TEXT NOT NULL,
    telegram_chat_id BIGINT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL,
    completed_task_count INTEGER NOT NULL,
    total_task_count INTEGER NOT NULL,
    completion_percentage NUMERIC(5,2) NOT NULL,
    incident_reported BOOLEAN NOT NULL DEFAULT FALSE,
    incident_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE coach_shift_checklist_submissions
    ADD COLUMN IF NOT EXISTS trial_first_name TEXT,
    ADD COLUMN IF NOT EXISTS trial_last_name TEXT,
    ADD COLUMN IF NOT EXISTS trial_email TEXT,
    ADD COLUMN IF NOT EXISTS trial_phone TEXT,
    ADD COLUMN IF NOT EXISTS trial_attendance TEXT;

CREATE TABLE IF NOT EXISTS coach_shift_checklist_tasks (
    id BIGSERIAL PRIMARY KEY,
    submission_id BIGINT NOT NULL REFERENCES coach_shift_checklist_submissions(id) ON DELETE CASCADE,
    section_name TEXT NOT NULL,
    task_name TEXT NOT NULL,
    completed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_coach_checklist_submissions_coach_name
    ON coach_shift_checklist_submissions (lower(coach_name));
CREATE INDEX IF NOT EXISTS idx_coach_checklist_submissions_username
    ON coach_shift_checklist_submissions (lower(telegram_username));
CREATE INDEX IF NOT EXISTS idx_coach_checklist_submissions_user_id
    ON coach_shift_checklist_submissions (telegram_user_id);
CREATE INDEX IF NOT EXISTS idx_coach_checklist_submissions_submitted_at
    ON coach_shift_checklist_submissions (submitted_at);
CREATE INDEX IF NOT EXISTS idx_coach_checklist_submissions_incident
    ON coach_shift_checklist_submissions (incident_reported);
CREATE INDEX IF NOT EXISTS idx_coach_checklist_submissions_chat_id
    ON coach_shift_checklist_submissions (telegram_chat_id);
"""


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", "/root/.hermes")).expanduser()


def state_path() -> Path:
    return hermes_home() / "state" / "coach_shift_checklist_sessions.json"


def db_name() -> str:
    return os.getenv("COACH_CHECKLIST_POSTGRES_DB") or os.getenv("PGDATABASE") or DEFAULT_DB_NAME


def management_chat_id() -> Optional[str]:
    return os.getenv("COACH_CHECKLIST_MANAGEMENT_CHAT_ID")


def configured_allowed_chat_ids(extra: Optional[Dict[str, Any]] = None) -> set[str]:
    extra = extra or {}
    raw_values: List[str] = []
    raw_cc = extra.get("coach_checklist")
    cc = raw_cc if isinstance(raw_cc, dict) else {}
    for value in [
        os.getenv("COACH_CHECKLIST_ALLOWED_CHAT_IDS"),
        cc.get("allowed_chat_ids"),
    ]:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            raw_values.extend(str(v) for v in value)
        else:
            raw_values.extend(str(value).split(","))
    return {v.strip() for v in raw_values if v and str(v).strip()}


def chat_allowed(chat_id: Any, extra: Optional[Dict[str, Any]] = None) -> bool:
    allowed = configured_allowed_chat_ids(extra)
    return bool(allowed) and str(chat_id) in allowed


def _sql_quote(value: Optional[Any]) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _run_psql(sql: str, *, capture: bool = True) -> str:
    cmd = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", db_name()]
    if capture:
        cmd.extend(["-Atq"])
    proc = subprocess.run(cmd, input=sql, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(f"psql failed: {stderr[:400]}")
    return proc.stdout or ""


def apply_migration() -> None:
    table = _run_psql("SELECT to_regclass('public.coach_shift_checklist_submissions');").strip()
    if not table:
        _run_psql(MIGRATION_SQL, capture=False)
        return
    required_columns = {"trial_first_name", "trial_last_name", "trial_email", "trial_phone", "trial_attendance"}
    existing = set(_run_psql("""
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'coach_shift_checklist_submissions';
""").splitlines())
    missing = required_columns - existing
    if not missing:
        return
    clauses = [f"ADD COLUMN IF NOT EXISTS {column} TEXT" for column in sorted(missing)]
    _run_psql("ALTER TABLE coach_shift_checklist_submissions\n    " + ",\n    ".join(clauses) + ";", capture=False)


@dataclass
class ChecklistSession:
    session_id: str
    telegram_user_id: int
    telegram_username: Optional[str]
    coach_name: str
    telegram_chat_id: int
    message_thread_id: Optional[int] = None
    message_id: Optional[int] = None
    current_section: int = 0
    completed: List[bool] = field(default_factory=lambda: [False] * TOTAL_TASKS)
    status: str = "awaiting_trial"  # awaiting_trial | awaiting_trial_attendance | active | review | awaiting_incident | submitted | cancelled
    trial_field_index: int = 0
    trial_first_name: Optional[str] = None
    trial_last_name: Optional[str] = None
    trial_email: Optional[str] = None
    trial_phone: Optional[str] = None
    trial_attendance: Optional[str] = None
    incident_notes: Optional[str] = None
    submitted_id: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def is_expired(self) -> bool:
        try:
            updated = datetime.fromisoformat(self.updated_at)
        except ValueError:
            return True
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated > SESSION_TTL

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChecklistSession":
        completed = list(data.get("completed") or [])
        if len(completed) < TOTAL_TASKS:
            completed.extend([False] * (TOTAL_TASKS - len(completed)))
        elif len(completed) > TOTAL_TASKS:
            completed = completed[:TOTAL_TASKS]
        data = {**data, "completed": completed}
        return cls(**data)


def _load_sessions() -> Dict[str, ChecklistSession]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        logger.warning("Coach checklist session state could not be read; starting empty", exc_info=True)
        return {}
    sessions: Dict[str, ChecklistSession] = {}
    changed = False
    for sid, data in (raw or {}).items():
        try:
            session = ChecklistSession.from_dict(data)
            if session.is_expired() or session.status in {"submitted", "cancelled"}:
                changed = True
                continue
            sessions[sid] = session
        except Exception:
            changed = True
            logger.warning("Dropping invalid coach checklist session state for id=%s", sid, exc_info=True)
    if changed:
        _save_sessions(sessions)
    return sessions


def _save_sessions(sessions: Dict[str, ChecklistSession]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({sid: s.to_dict() for sid, s in sessions.items()}, indent=2, sort_keys=True))
    tmp.replace(path)


def create_session(user: Any, chat_id: int, *, thread_id: Optional[int] = None, message_id: Optional[int] = None) -> ChecklistSession:
    sessions = _load_sessions()
    sid = secrets.token_urlsafe(6).replace("-", "_")[:8]
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    display = " ".join(part for part in [first, last] if part).strip()
    if not display:
        display = getattr(user, "full_name", None) or getattr(user, "username", None) or str(getattr(user, "id", "Coach"))
    session = ChecklistSession(
        session_id=sid,
        telegram_user_id=int(getattr(user, "id")),
        telegram_username=getattr(user, "username", None),
        coach_name=display,
        telegram_chat_id=int(chat_id),
        message_thread_id=int(thread_id) if thread_id is not None else None,
        message_id=int(message_id) if message_id is not None else None,
    )
    sessions[sid] = session
    _save_sessions(sessions)
    return session


def get_session(session_id: str) -> Optional[ChecklistSession]:
    return _load_sessions().get(session_id)


def save_session(session: ChecklistSession) -> None:
    sessions = _load_sessions()
    session.touch()
    sessions[session.session_id] = session
    _save_sessions(sessions)


def remove_session(session_id: str) -> None:
    sessions = _load_sessions()
    sessions.pop(session_id, None)
    _save_sessions(sessions)


def active_incident_session(chat_id: Any, user_id: Any) -> Optional[ChecklistSession]:
    for session in _load_sessions().values():
        if (
            session.status == "awaiting_incident"
            and str(session.telegram_chat_id) == str(chat_id)
            and str(session.telegram_user_id) == str(user_id)
        ):
            return session
    return None


def active_trial_session(chat_id: Any, user_id: Any) -> Optional[ChecklistSession]:
    for session in _load_sessions().values():
        if (
            session.status == "awaiting_trial"
            and str(session.telegram_chat_id) == str(chat_id)
            and str(session.telegram_user_id) == str(user_id)
        ):
            return session
    return None


def task_bounds(section_index: int) -> Tuple[int, int]:
    start = 0
    for idx, (_, tasks) in enumerate(CHECKLIST_SECTIONS):
        end = start + len(tasks)
        if idx == section_index:
            return start, end
        start = end
    raise IndexError(section_index)


def counts(session: ChecklistSession) -> Tuple[int, int, float]:
    complete = sum(1 for v in session.completed if v)
    pct = round((complete / TOTAL_TASKS) * 100, 2) if TOTAL_TASKS else 0.0
    return complete, TOTAL_TASKS, pct


TRIAL_FIELDS: List[Tuple[str, str]] = [
    ("trial_first_name", "First name"),
    ("trial_last_name", "Last name"),
    ("trial_email", "Email address"),
    ("trial_phone", "Phone number"),
]


def _trial_label(value: Optional[str]) -> str:
    if value == "showed":
        return "Trial showed"
    if value == "no_show":
        return "No-show"
    return "—"


def render_trial_prompt(session: ChecklistSession) -> str:
    idx = max(0, min(session.trial_field_index, len(TRIAL_FIELDS) - 1))
    _, label = TRIAL_FIELDS[idx]
    lines = [
        "🥋 Coach On Shift Checklist",
        "Section 1: Trials / Walk-ins",
        "",
        "Enter trial or walk-in student information before starting the shift checklist.",
        "If there is no trial/walk-in, reply `skip`.",
        "",
        f"First name: {session.trial_first_name or '—'}",
        f"Last name: {session.trial_last_name or '—'}",
        f"Email address: {session.trial_email or '—'}",
        f"Phone number: {session.trial_phone or '—'}",
        f"Trial status: {_trial_label(session.trial_attendance)}",
        "",
        f"Please enter: {label}",
    ]
    return "\n".join(lines)


def render_trial_attendance_prompt(session: ChecklistSession) -> str:
    return "\n".join([
        "🥋 Coach On Shift Checklist",
        "Section 1: Trials / Walk-ins",
        "",
        f"Name: {' '.join(part for part in [session.trial_first_name, session.trial_last_name] if part).strip() or '—'}",
        f"Email: {session.trial_email or '—'}",
        f"Phone: {session.trial_phone or '—'}",
        "",
        "Select trial/walk-in status:",
    ])


def trial_attendance_keyboard(session: ChecklistSession, button_factory: Any) -> Any:
    showed = "✅ Trial showed" if session.trial_attendance == "showed" else "⬜️ Trial showed"
    no_show = "✅ No-show" if session.trial_attendance == "no_show" else "⬜️ No-show"
    return [[
        button_factory(showed, callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:trial:showed"),
        button_factory(no_show, callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:trial:no_show"),
    ]]


def handle_trial_input(session_id: str, text: str) -> Tuple[Optional[ChecklistSession], str]:
    session = get_session(session_id)
    if not session:
        return None, "expired"
    value = (text or "").strip()
    if value.lower() in {"skip", "none", "no", "n/a", "na"}:
        session.status = "active"
        session.trial_field_index = 0
        save_session(session)
        return session, "complete"
    if session.status != "awaiting_trial":
        return session, "ignored"
    idx = max(0, min(session.trial_field_index, len(TRIAL_FIELDS) - 1))
    attr, _ = TRIAL_FIELDS[idx]
    setattr(session, attr, value)
    if idx >= len(TRIAL_FIELDS) - 1:
        session.status = "awaiting_trial_attendance"
        session.trial_field_index = len(TRIAL_FIELDS)
        save_session(session)
        return session, "attendance"
    session.trial_field_index = idx + 1
    save_session(session)
    return session, "next"


def handle_trial_attendance(session_id: str, value: str) -> Tuple[Optional[ChecklistSession], str]:
    session = get_session(session_id)
    if not session:
        return None, "expired"
    if value not in {"showed", "no_show"}:
        return session, "unknown"
    session.trial_attendance = value
    session.status = "active"
    save_session(session)
    return session, "complete"


def render_section(session: ChecklistSession) -> str:
    idx = session.current_section
    title, tasks = CHECKLIST_SECTIONS[idx]
    start, _ = task_bounds(idx)
    complete, total, pct = counts(session)
    lines = [
        "🥋 Coach On Shift Checklist",
        f"Section {idx + 1} of {len(CHECKLIST_SECTIONS)}: {title}",
        "",
    ]
    for offset, task in enumerate(tasks):
        mark = "✅" if session.completed[start + offset] else "⬜️"
        lines.append(f"{mark} {task}")
    lines.extend(["", f"Progress: {complete}/{total} ({pct:.0f}%)"])
    return "\n".join(lines)


def render_review(session: ChecklistSession) -> str:
    complete, total, pct = counts(session)
    now = datetime.now(timezone.utc).astimezone(CT)
    username = f"@{session.telegram_username}" if session.telegram_username else "—"
    return "\n".join([
        "📋 Review Coach Checklist",
        "",
        f"Coach name: {session.coach_name}",
        f"Telegram username: {username}",
        f"Submission date: {now:%Y-%m-%d}",
        f"Submission time: {now:%I:%M %p %Z}",
        f"Completed tasks: {complete}",
        f"Total tasks: {total}",
        f"Completion percentage: {pct:.0f}%",
        "",
        "Trials / Walk-ins:",
        f"First name: {session.trial_first_name or '—'}",
        f"Last name: {session.trial_last_name or '—'}",
        f"Email address: {session.trial_email or '—'}",
        f"Phone number: {session.trial_phone or '—'}",
        f"Trial status: {_trial_label(session.trial_attendance)}",
        "",
        f"Incident reported: {'Yes' if session.incident_notes else 'No'}",
    ])


def section_keyboard(session: ChecklistSession, button_factory: Any) -> Any:
    idx = session.current_section
    _, tasks = CHECKLIST_SECTIONS[idx]
    start, _ = task_bounds(idx)
    rows = []
    for offset, task in enumerate(tasks):
        global_idx = start + offset
        mark = "✅" if session.completed[global_idx] else "⬜️"
        rows.append([button_factory(f"{mark} {task[:44]}", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:t:{global_idx}")])
    rows.append([
        button_factory("Select All", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:all"),
        button_factory("Clear Section", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:clr"),
    ])
    nav = []
    if idx > 0:
        nav.append(button_factory("Back", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:back"))
    if idx < len(CHECKLIST_SECTIONS) - 1:
        nav.append(button_factory("Next", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:next"))
    else:
        nav.append(button_factory("Review", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:review"))
    nav.append(button_factory("Cancel", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:cancel"))
    rows.append(nav)
    return rows


def review_keyboard(session: ChecklistSession, button_factory: Any) -> Any:
    return [
        [button_factory("Submit Checklist", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:submit")],
        [button_factory("Go Back", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:back")],
        [button_factory("Report Incident", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:incident")],
        [button_factory("Cancel", callback_data=f"{CALLBACK_PREFIX}:{session.session_id}:cancel")],
    ]


def handle_action(session_id: str, action: str, arg: Optional[str] = None) -> Tuple[Optional[ChecklistSession], str]:
    session = get_session(session_id)
    if not session:
        return None, "expired"
    if session.submitted_id or session.status == "submitted":
        return session, "already_submitted"
    if action == "t" and arg is not None:
        index = int(arg)
        if 0 <= index < TOTAL_TASKS:
            session.completed[index] = not session.completed[index]
    elif action == "all":
        start, end = task_bounds(session.current_section)
        for i in range(start, end):
            session.completed[i] = True
    elif action == "clr":
        start, end = task_bounds(session.current_section)
        for i in range(start, end):
            session.completed[i] = False
    elif action == "next":
        session.current_section = min(session.current_section + 1, len(CHECKLIST_SECTIONS) - 1)
    elif action == "back":
        if session.status == "review":
            session.status = "active"
            session.current_section = len(CHECKLIST_SECTIONS) - 1
        else:
            session.current_section = max(session.current_section - 1, 0)
    elif action == "review":
        session.status = "review"
    elif action == "incident":
        session.status = "awaiting_incident"
    elif action == "cancel":
        session.status = "cancelled"
    else:
        return session, "unknown"
    save_session(session)
    return session, "ok"


def persist_submission(session: ChecklistSession) -> int:
    if session.submitted_id:
        return session.submitted_id
    apply_migration()
    complete, total, pct = counts(session)
    submitted_at = datetime.now(timezone.utc).isoformat()
    incident = bool(session.incident_notes)
    sql = f"""
WITH inserted AS (
  INSERT INTO coach_shift_checklist_submissions (
    telegram_user_id, telegram_username, coach_name, telegram_chat_id,
    trial_first_name, trial_last_name, trial_email, trial_phone, trial_attendance,
    submitted_at, completed_task_count, total_task_count, completion_percentage,
    incident_reported, incident_notes
  ) VALUES (
    {int(session.telegram_user_id)}, {_sql_quote(session.telegram_username)}, {_sql_quote(session.coach_name)}, {int(session.telegram_chat_id)},
    {_sql_quote(session.trial_first_name)}, {_sql_quote(session.trial_last_name)}, {_sql_quote(session.trial_email)}, {_sql_quote(session.trial_phone)}, {_sql_quote(session.trial_attendance)},
    {_sql_quote(submitted_at)}, {complete}, {total}, {pct:.2f}, {'TRUE' if incident else 'FALSE'}, {_sql_quote(session.incident_notes)}
  ) RETURNING id
)
SELECT id FROM inserted;
"""
    out = _run_psql(sql).strip().splitlines()
    if not out:
        raise RuntimeError("submission insert returned no id")
    submission_id = int(out[-1])
    values = []
    flat_index = 0
    for section, tasks in CHECKLIST_SECTIONS:
        for task in tasks:
            values.append(
                f"({submission_id}, {_sql_quote(section)}, {_sql_quote(task)}, {'TRUE' if session.completed[flat_index] else 'FALSE'})"
            )
            flat_index += 1
    _run_psql(
        "INSERT INTO coach_shift_checklist_tasks (submission_id, section_name, task_name, completed) VALUES\n"
        + ",\n".join(values)
        + ";",
        capture=False,
    )
    session.submitted_id = submission_id
    session.status = "submitted"
    save_session(session)
    remove_session(session.session_id)
    return submission_id


def confirmation_text(session: ChecklistSession) -> str:
    complete, total, pct = counts(session)
    now = datetime.now(timezone.utc).astimezone(CT)
    return "\n".join([
        "✅ Coach checklist submitted",
        "",
        f"Coach: {session.coach_name}",
        f"Completed: {complete} of {total} tasks",
        f"Completion: {pct:.0f}%",
        f"Trial/walk-in: {' '.join(part for part in [session.trial_first_name, session.trial_last_name] if part).strip() or '—'}",
        f"Trial status: {_trial_label(session.trial_attendance)}",
        f"Incident reported: {'Yes' if session.incident_notes else 'No'}",
        f"Submitted: {now:%Y-%m-%d %I:%M %p %Z}",
    ])


def incident_alert_text(session: ChecklistSession) -> str:
    complete, total, pct = counts(session)
    now = datetime.now(timezone.utc).astimezone(CT)
    username = f"@{session.telegram_username}" if session.telegram_username else "—"
    return "\n".join([
        "🚨 INCIDENT REPORTED",
        "",
        f"Coach name: {session.coach_name}",
        f"Telegram username: {username}",
        f"Date and time: {now:%Y-%m-%d %I:%M %p %Z}",
        f"Completed task percentage: {pct:.0f}%",
        "",
        "Incident description:",
        session.incident_notes or "",
    ])


def incident_prompt() -> str:
    return "\n".join([
        "Describe the incident using facts only.",
        "",
        "Include:",
        "- Who was involved",
        "- What happened",
        "- Where it happened",
        "- Whether anyone was injured",
        "- What action was taken",
    ])


def recent_submissions_text(filter_arg: Optional[str] = None) -> str:
    apply_migration()
    where = []
    label = "Recent coach checklist submissions"
    arg = (filter_arg or "").strip()
    if arg.lower() == "today":
        where.append("submitted_at >= date_trunc('day', now() AT TIME ZONE 'America/Chicago') AT TIME ZONE 'America/Chicago'")
        label = "Today's coach checklist submissions"
    elif arg.lower() == "incidents":
        where.append("incident_reported IS TRUE")
        label = "Recent coach checklist incidents"
    elif arg:
        like = _sql_quote(f"%{arg.lower()}%")
        where.append(f"lower(coach_name) LIKE {like}")
        label = f"Recent checklist submissions for {arg}"
    sql = """
SELECT id || E'\t' || coach_name || E'\t' ||
       to_char(submitted_at AT TIME ZONE 'America/Chicago', 'YYYY-MM-DD HH12:MI AM TZ') || E'\t' ||
       completion_percentage::text || E'\t' ||
       CASE WHEN incident_reported THEN 'Yes' ELSE 'No' END
FROM coach_shift_checklist_submissions
"""
    if where:
        sql += "WHERE " + " AND ".join(where) + "\n"
    sql += "ORDER BY submitted_at DESC LIMIT 15;\n"
    rows = [line.split("\t") for line in _run_psql(sql).splitlines() if line.strip()]
    if not rows:
        return f"{label}\n\nNo submissions found."
    lines = [label, ""]
    for rid, coach, submitted, pct, incident in rows:
        lines.append(f"#{rid} — {coach}")
        lines.append(f"Date: {submitted} CT")
        lines.append(f"Completion: {float(pct):.0f}%")
        lines.append(f"Incident: {incident}")
        lines.append("")
    return "\n".join(lines).rstrip()


def export_csv() -> Path:
    apply_migration()
    out = _run_psql(
        """
SELECT id, coach_name, COALESCE(telegram_username, ''), telegram_user_id, telegram_chat_id,
       COALESCE(trial_first_name, ''), COALESCE(trial_last_name, ''), COALESCE(trial_email, ''), COALESCE(trial_phone, ''), COALESCE(trial_attendance, ''),
       submitted_at, completed_task_count, total_task_count, completion_percentage,
       incident_reported, COALESCE(incident_notes, '')
FROM coach_shift_checklist_submissions
ORDER BY submitted_at DESC;
"""
    )
    path = Path(tempfile.gettempdir()) / f"coach_checklist_export_{datetime.now(CT):%Y%m%d_%H%M%S}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Submission ID",
            "Coach name",
            "Telegram username",
            "Telegram user ID",
            "Telegram chat ID",
            "Trial first name",
            "Trial last name",
            "Trial email address",
            "Trial phone number",
            "Trial showed/no-show",
            "Submitted at",
            "Completed task count",
            "Total task count",
            "Completion percentage",
            "Incident reported",
            "Incident notes",
        ])
        for line in out.splitlines():
            writer.writerow(line.split("|"))
    return path
