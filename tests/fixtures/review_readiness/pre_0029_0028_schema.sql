-- Fixed copy of the 0028 table shapes used by the 0029 upgrade tests.
-- This asset must not be regenerated from the current SQLAlchemy metadata.
CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO schema_migrations(version, description) VALUES
    ('0026_write_operation_ledger', 'fixed pre-0029 fixture'),
    ('0027_context_projector_manifest_v2', 'fixed pre-0029 fixture'),
    ('0028_scoped_tool_authority', 'fixed pre-0029 fixture');

CREATE TABLE applications (
    id INTEGER PRIMARY KEY,
    company_name VARCHAR NOT NULL,
    position_name VARCHAR NOT NULL,
    job_url VARCHAR NOT NULL DEFAULT '',
    status VARCHAR NOT NULL DEFAULT 'applied',
    source VARCHAR NOT NULL DEFAULT 'cli',
    notes VARCHAR NOT NULL DEFAULT '',
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    first_pending_at DATETIME,
    first_applied_at DATETIME,
    first_written_test_at DATETIME,
    first_interview_at DATETIME,
    first_offer_at DATETIME,
    closed_reason VARCHAR NOT NULL DEFAULT '',
    closed_at DATETIME,
    deleted_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE application_events (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    event_type VARCHAR NOT NULL,
    subtype VARCHAR NOT NULL DEFAULT '',
    tags VARCHAR NOT NULL DEFAULT '[]',
    round INTEGER NOT NULL DEFAULT 0,
    scheduled_at DATETIME,
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    location VARCHAR NOT NULL DEFAULT '',
    notes VARCHAR NOT NULL DEFAULT '',
    remind_at DATETIME,
    status VARCHAR NOT NULL DEFAULT 'todo',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE interview_notes (
    id INTEGER PRIMARY KEY,
    application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    application_event_id INTEGER REFERENCES application_events(id) ON DELETE SET NULL,
    company VARCHAR NOT NULL,
    position VARCHAR NOT NULL,
    round VARCHAR NOT NULL DEFAULT '',
    date VARCHAR NOT NULL DEFAULT '',
    questions VARCHAR NOT NULL DEFAULT '',
    self_reflection VARCHAR NOT NULL DEFAULT '',
    difficulty_points VARCHAR NOT NULL DEFAULT '',
    mood VARCHAR NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE interview_review_proposals (
    id INTEGER PRIMARY KEY,
    note_id INTEGER REFERENCES interview_notes(id) ON DELETE SET NULL,
    application_event_id INTEGER REFERENCES application_events(id) ON DELETE SET NULL,
    idempotency_key VARCHAR NOT NULL,
    input_snapshot_json VARCHAR NOT NULL,
    source_fingerprint VARCHAR NOT NULL,
    proposal_json VARCHAR NOT NULL,
    proposal_hash VARCHAR NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_interview_review_proposals_note_key UNIQUE(note_id, idempotency_key)
);

CREATE TABLE interview_story_proposal_attempts (
    id INTEGER PRIMARY KEY,
    target_story_id INTEGER,
    idempotency_key VARCHAR NOT NULL,
    entrypoint VARCHAR NOT NULL,
    entry_context_json TEXT NOT NULL DEFAULT '{}',
    attempt_status VARCHAR NOT NULL,
    generation_revision INTEGER NOT NULL DEFAULT 1,
    provider_call_token VARCHAR NOT NULL DEFAULT '',
    provider_lease_until DATETIME,
    input_snapshot_json TEXT NOT NULL,
    source_fingerprint VARCHAR NOT NULL,
    proposal_json TEXT NOT NULL DEFAULT '',
    proposal_hash VARCHAR NOT NULL DEFAULT '',
    repair_count INTEGER NOT NULL DEFAULT 0,
    failure_category VARCHAR NOT NULL DEFAULT '',
    confirmation_token_hash VARCHAR NOT NULL DEFAULT '',
    confirmation_payload_hash VARCHAR NOT NULL DEFAULT '',
    confirmed_story_id INTEGER,
    confirmed_story_version_id INTEGER,
    confirmed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_interview_story_attempt_key UNIQUE(idempotency_key)
);

CREATE TABLE adaptive_practice_plans (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL,
    application_event_id INTEGER NOT NULL,
    interview_note_id INTEGER NOT NULL,
    interview_review_proposal_id INTEGER NOT NULL,
    focus_id VARCHAR NOT NULL,
    start_idempotency_key VARCHAR NOT NULL,
    start_input_fingerprint VARCHAR NOT NULL,
    source_fingerprint VARCHAR NOT NULL,
    source_path VARCHAR NOT NULL,
    source_excerpt TEXT NOT NULL,
    source_hash VARCHAR NOT NULL,
    drill_kind VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    observation TEXT NOT NULL,
    reason TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'in_progress',
    revision INTEGER NOT NULL DEFAULT 1,
    response_text TEXT NOT NULL DEFAULT '',
    reflection_text TEXT NOT NULL DEFAULT '',
    self_assessment VARCHAR NOT NULL DEFAULT '',
    completion_idempotency_key VARCHAR,
    completion_fingerprint VARCHAR NOT NULL DEFAULT '',
    completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_adaptive_practice_start_key UNIQUE(start_idempotency_key),
    CONSTRAINT uq_adaptive_practice_proposal_focus
        UNIQUE(interview_review_proposal_id, focus_id),
    CONSTRAINT uq_adaptive_practice_completion_key UNIQUE(completion_idempotency_key)
);

CREATE TABLE write_operations (
    id VARCHAR(36) PRIMARY KEY,
    operation_role VARCHAR NOT NULL,
    parent_operation_id VARCHAR(36) REFERENCES write_operations(id) ON DELETE RESTRICT,
    parent_terminal_payload_sha256 VARCHAR(71),
    conversation_id INTEGER,
    agent_run_id VARCHAR(36),
    tool_call_id VARCHAR,
    tool_name VARCHAR NOT NULL,
    adapter_kind VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    fingerprint_key_id VARCHAR(36) NOT NULL,
    proposal_fingerprint VARCHAR,
    input_fingerprint VARCHAR,
    confirmation_token_fingerprint VARCHAR,
    authorization_scope_fingerprint VARCHAR,
    operation_request_fingerprint VARCHAR,
    result_contract VARCHAR,
    result_json TEXT,
    visible_result TEXT,
    transport_json TEXT,
    undo_json TEXT,
    terminal_payload_sha256 VARCHAR,
    failure_category VARCHAR,
    failure_code VARCHAR,
    delivery_status VARCHAR NOT NULL,
    delivery_failure_code VARCHAR,
    delivery_outcome VARCHAR,
    delivery_message_count INTEGER,
    delivery_manifest_sha256 VARCHAR,
    delivery_next_operation_id VARCHAR(36)
        REFERENCES write_operations(id) ON DELETE RESTRICT,
    delivery_generation INTEGER NOT NULL,
    delivery_owner_token_fingerprint VARCHAR,
    delivery_lease_expires_at INTEGER,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at DATETIME,
    claimed_at DATETIME,
    rejected_at DATETIME,
    committed_at DATETIME,
    failed_at DATETIME,
    delivered_at DATETIME,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_write_operations_0028_manifest CHECK (
        (operation_role='primary' AND adapter_kind='typed' AND tool_name IN (
          'create_application','update_application_status','create_application_event',
          'update_application_event','delete_application_event','add_note','update_note',
          'delete_note','update_offer','save_offer_assessment',
          'resume_update_career_intent','resume_rewrite_highlight'))
        OR (operation_role='primary' AND adapter_kind='legacy_deterministic'
          AND tool_name IN ('save_application_jd_version',
          'create_application_submission_snapshot','record_application_outcome'))
        OR (operation_role='compensation' AND adapter_kind='compensation'
          AND tool_name IN ('undo:update_application_status','undo:create_application',
          'undo:create_application_event','undo:add_note'))
    ),
    CONSTRAINT ck_write_operations_0028_scope CHECK (
        NOT (operation_role='primary' AND adapter_kind='typed' AND status='proposed'
             AND authorization_scope_fingerprint IS NULL)
    )
);

CREATE TABLE write_operation_transitions (
    id VARCHAR(36) PRIMARY KEY,
    operation_id VARCHAR(36) NOT NULL
        REFERENCES write_operations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    state VARCHAR NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_write_operation_transitions_seq UNIQUE(operation_id, seq)
);
