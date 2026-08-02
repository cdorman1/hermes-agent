-- 20260801 Coach shift checklist tables
-- Stores completed Krav Maga Force Coach On Shift Checklist submissions.

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
