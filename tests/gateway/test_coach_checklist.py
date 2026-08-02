from __future__ import annotations

import os
import subprocess
import types

import pytest

from gateway import coach_checklist as cc


class User:
    id = 12345
    username = "coachuser"
    first_name = "Coach"
    last_name = "One"


def test_starting_checklist_creates_persisted_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = cc.create_session(User(), 7169074832)
    assert session.coach_name == "Coach One"
    assert session.telegram_username == "coachuser"
    assert cc.get_session(session.session_id) is not None
    assert "Student and Floor Presence" in cc.render_section(session)


def test_trial_walk_in_collection_starts_before_task_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = cc.create_session(User(), 7169074832)
    assert session.status == "awaiting_trial"
    assert session.trial_field_index == 0
    assert "Trials / Walk-ins" in cc.render_trial_prompt(session)
    assert "First name" in cc.render_trial_prompt(session)

    session, result = cc.handle_trial_input(session.session_id, "Jane")
    assert result == "next"
    assert session.trial_first_name == "Jane"
    assert "Last name" in cc.render_trial_prompt(session)

    session, result = cc.handle_trial_input(session.session_id, "Doe")
    assert result == "next"
    session, result = cc.handle_trial_input(session.session_id, "jane@example.com")
    assert result == "next"
    session, result = cc.handle_trial_input(session.session_id, "555-123-4567")
    assert result == "attendance"
    assert session.status == "awaiting_trial_attendance"
    assert session.trial_last_name == "Doe"
    assert session.trial_email == "jane@example.com"
    assert session.trial_phone == "555-123-4567"
    buttons = cc.trial_attendance_keyboard(session, lambda text, callback_data: {"text": text, "callback_data": callback_data})
    assert "Trial showed" in buttons[0][0]["text"]
    assert "No-show" in buttons[0][1]["text"]
    session, result = cc.handle_trial_attendance(session.session_id, "showed")
    assert result == "complete"
    assert session.status == "active"
    assert session.trial_attendance == "showed"
    assert "Student and Floor Presence" in cc.render_section(session)


def test_trial_walk_in_collection_can_be_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = cc.create_session(User(), 7169074832)
    session, result = cc.handle_trial_input(session.session_id, "skip")
    assert result == "complete"
    assert session.status == "active"
    assert session.trial_first_name is None
    assert "Student and Floor Presence" in cc.render_section(session)


def test_toggling_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = cc.create_session(User(), 7169074832)
    session.status = "active"
    cc.save_session(session)
    session, result = cc.handle_action(session.session_id, "t", "0")
    assert result == "ok"
    assert session is not None
    assert session.completed[0] is True
    session, _ = cc.handle_action(session.session_id, "t", "0")
    assert session.completed[0] is False


def test_select_all_tasks_for_section(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = cc.create_session(User(), 7169074832)
    session, _ = cc.handle_action(session.session_id, "all")
    start, end = cc.task_bounds(0)
    assert all(session.completed[start:end])
    assert not any(session.completed[end:])


def test_clear_section(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = cc.create_session(User(), 7169074832)
    session, _ = cc.handle_action(session.session_id, "all")
    session, _ = cc.handle_action(session.session_id, "clr")
    start, end = cc.task_bounds(0)
    assert not any(session.completed[start:end])


def test_navigating_between_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = cc.create_session(User(), 7169074832)
    session, _ = cc.handle_action(session.session_id, "next")
    assert session.current_section == 1
    session, _ = cc.handle_action(session.session_id, "back")
    assert session.current_section == 0


def test_submit_prevents_duplicate_submissions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []

    def fake_run(sql, capture=True):
        calls.append(sql)
        if "RETURNING id" in sql:
            return "42\n"
        return ""

    monkeypatch.setattr(cc, "_run_psql", fake_run)
    session = cc.create_session(User(), 7169074832)
    session.status = "active"
    session.trial_first_name = "Jane"
    session.trial_last_name = "Doe"
    session.trial_email = "jane@example.com"
    session.trial_phone = "555-123-4567"
    session.trial_attendance = "showed"
    first = cc.persist_submission(session)
    second = cc.persist_submission(session)
    assert first == 42
    assert second == 42
    insert_sql = next(call for call in calls if "RETURNING id" in call)
    assert "trial_first_name" in insert_sql
    assert "Jane" in insert_sql
    assert "jane@example.com" in insert_sql
    assert sum("RETURNING id" in call for call in calls) == 1


def test_reporting_incident_sets_incident_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cc, "_run_psql", lambda sql, capture=True: "77\n" if "RETURNING id" in sql else "")
    session = cc.create_session(User(), 7169074832)
    session, result = cc.handle_action(session.session_id, "incident")
    assert result == "ok"
    assert session.status == "awaiting_incident"
    session.incident_notes = "Student slipped on the mat. No injury. Coach separated activity and cleaned area."
    cc.persist_submission(session)
    assert session.submitted_id == 77
    assert "INCIDENT REPORTED" in cc.incident_alert_text(session)


def test_unauthorized_channel_access(monkeypatch):
    monkeypatch.setenv("COACH_CHECKLIST_ALLOWED_CHAT_IDS", "111,222")
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    assert cc.chat_allowed(111)
    assert not cc.chat_allowed(333)


def test_csv_export(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake_run(sql, capture=True):
        if "SELECT id, coach_name" in sql:
            return "1|Coach One|coachuser|12345|7169074832|Jane|Doe|jane@example.com|555-123-4567|showed|2026-08-01 12:00:00+00|3|31|9.68|f|\n"
        return ""

    monkeypatch.setattr(cc, "_run_psql", fake_run)
    path = cc.export_csv()
    text = path.read_text()
    assert "Submission ID,Coach name,Telegram username" in text
    assert "Coach One" in text


@pytest.mark.skipif(not os.path.exists("/usr/bin/psql"), reason="psql not installed")
def test_postgresql_persistence_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("COACH_CHECKLIST_POSTGRES_DB", os.getenv("COACH_CHECKLIST_POSTGRES_DB", "postgres"))
    try:
        subprocess.run(["psql", "-X", "-d", cc.db_name(), "-Atqc", "select 1"], check=True, capture_output=True, text=True)
        can_create = subprocess.run(
            ["psql", "-X", "-d", cc.db_name(), "-Atqc", "select has_schema_privilege(current_user, 'public', 'CREATE')"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if can_create != "t":
            pytest.skip("PostgreSQL public schema is not writable for this test user")
    except Exception as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    session = cc.create_session(User(), 7169074832)
    session.completed[0] = True
    submission_id = cc.persist_submission(session)
    try:
        out = subprocess.run(
            ["psql", "-X", "-d", cc.db_name(), "-Atqc", f"select completed_task_count,total_task_count from coach_shift_checklist_submissions where id={submission_id}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert out == f"1|{cc.TOTAL_TASKS}"
    finally:
        subprocess.run(["psql", "-X", "-d", cc.db_name(), "-Atqc", f"delete from coach_shift_checklist_submissions where id={submission_id}"], check=False)
