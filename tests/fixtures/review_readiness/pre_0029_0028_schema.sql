BEGIN TRANSACTION;
CREATE TABLE adaptive_practice_plans (
	id INTEGER NOT NULL,
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
	status VARCHAR DEFAULT 'in_progress' NOT NULL,
	revision INTEGER DEFAULT '1' NOT NULL,
	response_text TEXT DEFAULT '' NOT NULL,
	reflection_text TEXT DEFAULT '' NOT NULL,
	self_assessment VARCHAR DEFAULT '' NOT NULL,
	completion_idempotency_key VARCHAR,
	completion_fingerprint VARCHAR DEFAULT '' NOT NULL,
	completed_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_adaptive_practice_start_key UNIQUE (start_idempotency_key),
	CONSTRAINT uq_adaptive_practice_proposal_focus UNIQUE (interview_review_proposal_id, focus_id),
	CONSTRAINT uq_adaptive_practice_completion_key UNIQUE (completion_idempotency_key)
);
CREATE TABLE agent_context_snapshots (
	id VARCHAR(36) NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	execution_segment_id VARCHAR(36) NOT NULL,
	snapshot_key VARCHAR NOT NULL,
	manifest_schema_version INTEGER DEFAULT '1' NOT NULL,
	snapshot_kind VARCHAR NOT NULL,
	model_step INTEGER,
	model_call_id VARCHAR(36),
	manifest_json TEXT NOT NULL,
	manifest_digest VARCHAR(64) NOT NULL,
	canonicalizer_version VARCHAR NOT NULL,
	logical_input_fingerprint VARCHAR(64) NOT NULL,
	fingerprint_key_id VARCHAR(36) NOT NULL,
	estimated_token_count INTEGER,
	token_estimator_name VARCHAR,
	token_estimator_version VARCHAR,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_agent_context_run_snapshot UNIQUE (run_id, snapshot_key),
	CONSTRAINT uq_agent_context_run_model_call UNIQUE (run_id, model_call_id),
	CONSTRAINT ck_agent_context_id_uuid CHECK (length(id) = 36 AND lower(id) = id AND substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-' AND length(replace(id, '-', '')) = 32 AND id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_agent_context_segment_uuid CHECK (length(execution_segment_id) = 36 AND lower(execution_segment_id) = execution_segment_id AND substr(execution_segment_id, 9, 1) = '-' AND substr(execution_segment_id, 14, 1) = '-' AND substr(execution_segment_id, 19, 1) = '-' AND substr(execution_segment_id, 24, 1) = '-' AND length(replace(execution_segment_id, '-', '')) = 32 AND execution_segment_id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_agent_context_model_call_uuid CHECK (model_call_id IS NULL OR (length(model_call_id) = 36 AND lower(model_call_id) = model_call_id AND substr(model_call_id, 9, 1) = '-' AND substr(model_call_id, 14, 1) = '-' AND substr(model_call_id, 19, 1) = '-' AND substr(model_call_id, 24, 1) = '-' AND length(replace(model_call_id, '-', '')) = 32 AND model_call_id NOT GLOB '*[^0-9a-f-]*')),
	CONSTRAINT ck_agent_context_key_uuid CHECK (length(fingerprint_key_id) = 36 AND lower(fingerprint_key_id) = fingerprint_key_id AND substr(fingerprint_key_id, 9, 1) = '-' AND substr(fingerprint_key_id, 14, 1) = '-' AND substr(fingerprint_key_id, 19, 1) = '-' AND substr(fingerprint_key_id, 24, 1) = '-' AND length(replace(fingerprint_key_id, '-', '')) = 32 AND fingerprint_key_id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_agent_context_manifest_schema CHECK (manifest_schema_version IN (1, 2)),
	CONSTRAINT ck_agent_context_manifest_size CHECK ((manifest_schema_version = 1 AND length(CAST(manifest_json AS BLOB)) <= 16384) OR (manifest_schema_version = 2 AND length(CAST(manifest_json AS BLOB)) <= 65536)),
	CONSTRAINT ck_agent_context_token_count CHECK (estimated_token_count IS NULL OR estimated_token_count >= 0),
	CONSTRAINT ck_agent_context_token_estimator_pair CHECK ((token_estimator_name IS NULL AND token_estimator_version IS NULL) OR (token_estimator_name IS NOT NULL AND token_estimator_version IS NOT NULL)),
	FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
);
CREATE TABLE agent_events (
	id VARCHAR(36) NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	seq INTEGER NOT NULL,
	dedupe_key VARCHAR NOT NULL,
	event_type VARCHAR NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	execution_segment_id VARCHAR(36) NOT NULL,
	model_step INTEGER,
	model_call_id VARCHAR(36),
	source_ref_type VARCHAR,
	source_ref_id VARCHAR,
	fingerprint_key_id VARCHAR(36),
	payload_json TEXT NOT NULL,
	payload_digest VARCHAR(64) NOT NULL,
	fact_digest VARCHAR(64) NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_agent_events_run_seq UNIQUE (run_id, seq),
	CONSTRAINT uq_agent_events_run_dedupe UNIQUE (run_id, dedupe_key),
	CONSTRAINT ck_agent_events_id_uuid CHECK (length(id) = 36 AND lower(id) = id AND substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-' AND length(replace(id, '-', '')) = 32 AND id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_agent_events_segment_uuid CHECK (length(execution_segment_id) = 36 AND lower(execution_segment_id) = execution_segment_id AND substr(execution_segment_id, 9, 1) = '-' AND substr(execution_segment_id, 14, 1) = '-' AND substr(execution_segment_id, 19, 1) = '-' AND substr(execution_segment_id, 24, 1) = '-' AND length(replace(execution_segment_id, '-', '')) = 32 AND execution_segment_id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_agent_events_model_call_uuid CHECK (model_call_id IS NULL OR (length(model_call_id) = 36 AND lower(model_call_id) = model_call_id AND substr(model_call_id, 9, 1) = '-' AND substr(model_call_id, 14, 1) = '-' AND substr(model_call_id, 19, 1) = '-' AND substr(model_call_id, 24, 1) = '-' AND length(replace(model_call_id, '-', '')) = 32 AND model_call_id NOT GLOB '*[^0-9a-f-]*')),
	CONSTRAINT ck_agent_events_seq CHECK (seq > 0),
	CONSTRAINT ck_agent_events_key_uuid CHECK (fingerprint_key_id IS NULL OR (length(fingerprint_key_id) = 36 AND lower(fingerprint_key_id) = fingerprint_key_id AND substr(fingerprint_key_id, 9, 1) = '-' AND substr(fingerprint_key_id, 14, 1) = '-' AND substr(fingerprint_key_id, 19, 1) = '-' AND substr(fingerprint_key_id, 24, 1) = '-' AND length(replace(fingerprint_key_id, '-', '')) = 32 AND fingerprint_key_id NOT GLOB '*[^0-9a-f-]*')),
	CONSTRAINT ck_agent_events_payload_size CHECK (length(CAST(payload_json AS BLOB)) <= 4096),
	FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
);
CREATE TABLE agent_runs (
	id VARCHAR(36) NOT NULL,
	conversation_id INTEGER NOT NULL,
	input_message_id INTEGER,
	origin_kind VARCHAR NOT NULL,
	initial_context_type VARCHAR NOT NULL,
	initial_context_entity_id VARCHAR(36),
	initial_context_ref_fingerprint VARCHAR(64),
	fingerprint_key_id VARCHAR(36) NOT NULL,
	initial_transport_mode VARCHAR NOT NULL,
	initial_route_kind VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	waiting_tool_call_id VARCHAR,
	last_seq INTEGER DEFAULT '0' NOT NULL,
	recording_status VARCHAR DEFAULT 'healthy' NOT NULL,
	recording_error_count INTEGER DEFAULT '0' NOT NULL,
	failure_code VARCHAR,
	started_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	finished_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT ck_agent_runs_id_uuid CHECK (length(id) = 36 AND lower(id) = id AND substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-' AND length(replace(id, '-', '')) = 32 AND id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_agent_runs_key_uuid CHECK (length(fingerprint_key_id) = 36 AND lower(fingerprint_key_id) = fingerprint_key_id AND substr(fingerprint_key_id, 9, 1) = '-' AND substr(fingerprint_key_id, 14, 1) = '-' AND substr(fingerprint_key_id, 19, 1) = '-' AND substr(fingerprint_key_id, 24, 1) = '-' AND length(replace(fingerprint_key_id, '-', '')) = 32 AND fingerprint_key_id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_agent_runs_last_seq CHECK (last_seq >= 0),
	CONSTRAINT ck_agent_runs_recording_error_count CHECK (recording_error_count >= 0),
	CONSTRAINT ck_agent_runs_status CHECK (status IN ('running','waiting_confirmation','completed','failed','cancelled','timed_out')),
	CONSTRAINT ck_agent_runs_recording_status CHECK (recording_status IN ('healthy','degraded')),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE,
	FOREIGN KEY(input_message_id) REFERENCES chat_messages (id) ON DELETE SET NULL
);
CREATE TABLE application_events (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	event_type VARCHAR NOT NULL,
	subtype VARCHAR DEFAULT '' NOT NULL,
	tags VARCHAR DEFAULT '[]' NOT NULL,
	round INTEGER DEFAULT '0' NOT NULL,
	scheduled_at DATETIME,
	duration_minutes INTEGER DEFAULT '0' NOT NULL,
	location VARCHAR DEFAULT '' NOT NULL,
	notes VARCHAR DEFAULT '' NOT NULL,
	remind_at DATETIME,
	status VARCHAR DEFAULT 'todo' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE
);
CREATE TABLE application_evidence_bundles (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	sequence INTEGER NOT NULL,
	submitted_at DATETIME NOT NULL,
	confirmed_at DATETIME NOT NULL,
	confirmation_kind VARCHAR DEFAULT 'user_asserted' NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	snapshot_json VARCHAR NOT NULL,
	bundle_sha256 VARCHAR NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_evidence_bundle_sequence UNIQUE (application_id, sequence),
	CONSTRAINT uq_evidence_bundle_idempotency UNIQUE (application_id, idempotency_key),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE
);
CREATE TABLE application_jd_versions (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	version_number INTEGER NOT NULL,
	jd_text TEXT NOT NULL,
	content_sha256 VARCHAR NOT NULL,
	source_url VARCHAR,
	source_kind VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	request_fingerprint_sha256 VARCHAR NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (application_id, version_number),
	UNIQUE (application_id, idempotency_key),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE
);
CREATE TABLE application_material_kits (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	resume_id INTEGER,
	jd_analysis_id INTEGER,
	jd_snapshot VARCHAR DEFAULT '' NOT NULL,
	jd_version_id INTEGER,
	status VARCHAR DEFAULT 'draft' NOT NULL,
	content_json VARCHAR DEFAULT '{}' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (application_id),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE,
	FOREIGN KEY(resume_id) REFERENCES resumes (id) ON DELETE SET NULL,
	FOREIGN KEY(jd_analysis_id) REFERENCES jd_analyses (id) ON DELETE SET NULL
);
CREATE TABLE application_outcomes (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	submission_snapshot_id INTEGER NOT NULL,
	application_event_id INTEGER,
	stage VARCHAR NOT NULL,
	result VARCHAR NOT NULL,
	feedback_text TEXT DEFAULT '' NOT NULL,
	reflection_text TEXT DEFAULT '' NOT NULL,
	next_action_text TEXT DEFAULT '' NOT NULL,
	feedback_tags_json TEXT DEFAULT '[]' NOT NULL,
	source_kind VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	request_fingerprint_sha256 VARCHAR NOT NULL,
	occurred_at DATETIME NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_application_outcomes_application_key UNIQUE (application_id, idempotency_key),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE,
	FOREIGN KEY(submission_snapshot_id) REFERENCES application_submission_snapshots (id) ON DELETE RESTRICT,
	FOREIGN KEY(application_event_id) REFERENCES application_events (id) ON DELETE SET NULL
);
CREATE TABLE application_submission_snapshots (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	resume_id INTEGER NOT NULL,
	jd_version_id INTEGER NOT NULL,
	material_kit_id INTEGER,
	resume_snapshot_json TEXT NOT NULL,
	resume_snapshot_hash VARCHAR NOT NULL,
	jd_snapshot TEXT NOT NULL,
	jd_snapshot_hash VARCHAR NOT NULL,
	material_snapshot_json TEXT,
	material_snapshot_hash VARCHAR,
	note TEXT DEFAULT '' NOT NULL,
	source_kind VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	request_fingerprint_sha256 VARCHAR NOT NULL,
	submitted_at DATETIME NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_application_submission_snapshots_application_key UNIQUE (application_id, idempotency_key),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE,
	FOREIGN KEY(resume_id) REFERENCES resumes (id) ON DELETE RESTRICT,
	FOREIGN KEY(jd_version_id) REFERENCES application_jd_versions (id) ON DELETE RESTRICT,
	FOREIGN KEY(material_kit_id) REFERENCES application_material_kits (id) ON DELETE RESTRICT
);
CREATE TABLE applications (
	id INTEGER NOT NULL,
	company_name VARCHAR NOT NULL,
	position_name VARCHAR NOT NULL,
	job_url VARCHAR DEFAULT '' NOT NULL,
	status VARCHAR DEFAULT 'applied' NOT NULL,
	source VARCHAR DEFAULT 'cli' NOT NULL,
	notes VARCHAR DEFAULT '' NOT NULL,
	applied_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	first_pending_at DATETIME,
	first_applied_at DATETIME,
	first_written_test_at DATETIME,
	first_interview_at DATETIME,
	first_offer_at DATETIME,
	closed_reason VARCHAR DEFAULT '' NOT NULL,
	closed_at DATETIME,
	deleted_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE chat_messages (
	id INTEGER NOT NULL,
	conversation_id INTEGER NOT NULL,
	role VARCHAR NOT NULL,
	content VARCHAR DEFAULT '' NOT NULL,
	tool_calls VARCHAR DEFAULT '' NOT NULL,
	tool_call_id VARCHAR DEFAULT '' NOT NULL,
	provider_blocks VARCHAR DEFAULT '' NOT NULL,
	operation_id VARCHAR(36),
	delivery_kind VARCHAR,
	delivery_ordinal INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_chat_messages_delivery_group CHECK ((operation_id IS NULL AND delivery_kind IS NULL AND delivery_ordinal IS NULL) OR (operation_id IS NOT NULL AND delivery_kind IS NOT NULL AND delivery_ordinal IS NOT NULL)),
	CONSTRAINT ck_chat_messages_delivery_shape CHECK (operation_id IS NULL OR (delivery_kind = 'origin_tool_result' AND delivery_ordinal = 0 AND role = 'tool' AND tool_call_id <> '') OR (delivery_kind = 'continuation_message' AND delivery_ordinal >= 1 AND ((role = 'tool' AND tool_call_id <> '') OR (role = 'assistant' AND tool_call_id = '')))),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE,
	FOREIGN KEY(operation_id) REFERENCES write_operations (id) ON DELETE RESTRICT
);
CREATE TABLE conversations (
	id INTEGER NOT NULL,
	title VARCHAR DEFAULT '新对话' NOT NULL,
	title_source VARCHAR DEFAULT 'fallback' NOT NULL,
	mode VARCHAR DEFAULT 'general' NOT NULL,
	context_type VARCHAR DEFAULT 'workspace' NOT NULL,
	context_ref VARCHAR DEFAULT '' NOT NULL,
	scope_revision INTEGER DEFAULT 0 NOT NULL,
	pinned_at DATETIME,
	archived_at DATETIME,
	pending_tool_call_id VARCHAR DEFAULT '' NOT NULL,
	pending_operation_id VARCHAR DEFAULT '' NOT NULL,
	pending_confirmation_claim_id VARCHAR DEFAULT '' NOT NULL,
	pending_confirmation_claimed_at DATETIME,
	pending_tool_name VARCHAR DEFAULT '' NOT NULL,
	pending_args VARCHAR DEFAULT '' NOT NULL,
	pending_human VARCHAR DEFAULT '' NOT NULL,
	clarification_tool_call_id VARCHAR DEFAULT '' NOT NULL,
	clarification_tool_name VARCHAR DEFAULT '' NOT NULL,
	clarification_args VARCHAR DEFAULT '' NOT NULL,
	clarification_human VARCHAR DEFAULT '' NOT NULL,
	clarification_question VARCHAR DEFAULT '' NOT NULL,
	last_write_undo_json VARCHAR DEFAULT '' NOT NULL,
	last_write_operation_id VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_conversations_scope_revision CHECK (typeof(scope_revision) = 'integer' AND scope_revision BETWEEN 0 AND 9223372036854775807)
);
CREATE TABLE interview_knowledge_capture_attempts (
	id INTEGER NOT NULL,
	note_id INTEGER NOT NULL,
	attempt_key VARCHAR NOT NULL,
	note_fingerprint VARCHAR NOT NULL,
	selected_fragments_json TEXT NOT NULL,
	last_preview_mode VARCHAR DEFAULT 'direct' NOT NULL,
	preview_status VARCHAR DEFAULT 'not_requested' NOT NULL,
	preview_revision INTEGER DEFAULT '0' NOT NULL,
	provider_call_token VARCHAR DEFAULT '' NOT NULL,
	preview_json TEXT DEFAULT '' NOT NULL,
	preview_error_code VARCHAR DEFAULT '' NOT NULL,
	confirmed_note_version_id INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	expires_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_interview_capture_note_key UNIQUE (note_id, attempt_key),
	FOREIGN KEY(note_id) REFERENCES interview_notes (id) ON DELETE CASCADE,
	FOREIGN KEY(confirmed_note_version_id) REFERENCES knowledge_note_versions (id) ON DELETE SET NULL
);
CREATE TABLE interview_notes (
	id INTEGER NOT NULL,
	application_id INTEGER,
	application_event_id INTEGER,
	company VARCHAR NOT NULL,
	position VARCHAR NOT NULL,
	round VARCHAR DEFAULT '' NOT NULL,
	date VARCHAR DEFAULT '' NOT NULL,
	questions VARCHAR DEFAULT '' NOT NULL,
	self_reflection VARCHAR DEFAULT '' NOT NULL,
	difficulty_points VARCHAR DEFAULT '' NOT NULL,
	mood VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE SET NULL,
	FOREIGN KEY(application_event_id) REFERENCES application_events (id) ON DELETE SET NULL
);
CREATE TABLE interview_practice_cases (
	id INTEGER NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	request_fingerprint_sha256 VARCHAR NOT NULL,
	position_name_snapshot VARCHAR NOT NULL,
	jd_text_snapshot TEXT NOT NULL,
	jd_fingerprint_sha256 VARCHAR NOT NULL,
	resume_id INTEGER NOT NULL,
	resume_content_snapshot_json TEXT NOT NULL,
	resume_fingerprint_sha256 VARCHAR NOT NULL,
	status VARCHAR DEFAULT 'active' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	archived_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_interview_practice_cases_key UNIQUE (idempotency_key),
	FOREIGN KEY(resume_id) REFERENCES resumes (id) ON DELETE RESTRICT
);
CREATE TABLE interview_preparation_proposals (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	application_event_id INTEGER NOT NULL,
	resume_id INTEGER NOT NULL,
	jd_version_id INTEGER,
	idempotency_key VARCHAR NOT NULL,
	attempt_status VARCHAR DEFAULT 'generating' NOT NULL,
	proposal_status VARCHAR DEFAULT '' NOT NULL,
	generation_revision INTEGER DEFAULT '1' NOT NULL,
	provider_call_token VARCHAR DEFAULT '' NOT NULL,
	provider_lease_until DATETIME,
	invalidation_reason VARCHAR DEFAULT '' NOT NULL,
	input_snapshot_json TEXT DEFAULT '' NOT NULL,
	source_fingerprint VARCHAR DEFAULT '' NOT NULL,
	proposal_json TEXT DEFAULT '' NOT NULL,
	proposal_hash VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_interview_preparation_application_event_key UNIQUE (application_id, application_event_id, idempotency_key)
);
CREATE TABLE interview_review_proposals (
	id INTEGER NOT NULL,
	note_id INTEGER,
	application_event_id INTEGER,
	idempotency_key VARCHAR NOT NULL,
	input_snapshot_json VARCHAR NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	proposal_json VARCHAR NOT NULL,
	proposal_hash VARCHAR NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_interview_review_proposals_note_key UNIQUE (note_id, idempotency_key),
	FOREIGN KEY(note_id) REFERENCES interview_notes (id) ON DELETE SET NULL,
	FOREIGN KEY(application_event_id) REFERENCES application_events (id) ON DELETE SET NULL
);
CREATE TABLE interview_stories (
	id INTEGER NOT NULL,
	title TEXT DEFAULT '' NOT NULL,
	status VARCHAR DEFAULT 'active' NOT NULL,
	current_version_id INTEGER,
	story_revision INTEGER DEFAULT '1' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	archived_at DATETIME,
	PRIMARY KEY (id)
);
CREATE TABLE interview_story_proposal_attempts (
	id INTEGER NOT NULL,
	target_story_id INTEGER,
	idempotency_key VARCHAR NOT NULL,
	entrypoint VARCHAR NOT NULL,
	entry_context_json TEXT DEFAULT '{}' NOT NULL,
	attempt_status VARCHAR NOT NULL,
	generation_revision INTEGER DEFAULT '1' NOT NULL,
	provider_call_token VARCHAR DEFAULT '' NOT NULL,
	provider_lease_until DATETIME,
	input_snapshot_json TEXT NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	proposal_json TEXT DEFAULT '' NOT NULL,
	proposal_hash VARCHAR DEFAULT '' NOT NULL,
	repair_count INTEGER DEFAULT '0' NOT NULL,
	failure_category VARCHAR DEFAULT '' NOT NULL,
	confirmation_token_hash VARCHAR DEFAULT '' NOT NULL,
	confirmation_payload_hash VARCHAR DEFAULT '' NOT NULL,
	confirmed_story_id INTEGER,
	confirmed_story_version_id INTEGER,
	confirmed_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_interview_story_attempt_key UNIQUE (idempotency_key)
);
CREATE TABLE interview_story_user_assertions (
	id INTEGER NOT NULL,
	story_version_id INTEGER NOT NULL,
	statement_text TEXT NOT NULL,
	statement_hash VARCHAR NOT NULL,
	confirmed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_interview_story_assertion_hash UNIQUE (story_version_id, statement_hash),
	FOREIGN KEY(story_version_id) REFERENCES interview_story_versions (id) ON DELETE RESTRICT
);
CREATE TABLE interview_story_version_evidence_links (
	id INTEGER NOT NULL,
	story_version_id INTEGER NOT NULL,
	target_kind VARCHAR NOT NULL,
	target_id VARCHAR NOT NULL,
	source_kind VARCHAR NOT NULL,
	source_stable_id VARCHAR NOT NULL,
	source_version_or_snapshot VARCHAR DEFAULT '' NOT NULL,
	source_path VARCHAR NOT NULL,
	text_location VARCHAR DEFAULT '' NOT NULL,
	excerpt TEXT NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	link_hash VARCHAR NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_interview_story_evidence_link_identity UNIQUE (story_version_id, target_kind, target_id, source_kind, source_stable_id, source_version_or_snapshot, source_path, text_location, excerpt),
	FOREIGN KEY(story_version_id) REFERENCES interview_story_versions (id) ON DELETE RESTRICT
);
CREATE TABLE interview_story_versions (
	id INTEGER NOT NULL,
	story_id INTEGER NOT NULL,
	version_number INTEGER NOT NULL,
	content_json TEXT NOT NULL,
	content_hash VARCHAR NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	origin_kind VARCHAR NOT NULL,
	confirmed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_interview_story_versions_story_number UNIQUE (story_id, version_number),
	FOREIGN KEY(story_id) REFERENCES interview_stories (id) ON DELETE RESTRICT
);
CREATE TABLE jd_analyses (
	id INTEGER NOT NULL,
	application_id INTEGER,
	jd_source VARCHAR DEFAULT 'text' NOT NULL,
	jd_text VARCHAR NOT NULL,
	result VARCHAR NOT NULL,
	jd_version_id INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE SET NULL
);
CREATE TABLE knowledge_brief_attempt_steps (
	id INTEGER NOT NULL,
	attempt_id INTEGER NOT NULL,
	sequence INTEGER NOT NULL,
	iteration INTEGER DEFAULT '0' NOT NULL,
	phase VARCHAR NOT NULL,
	status VARCHAR DEFAULT 'completed' NOT NULL,
	block_path VARCHAR DEFAULT '' NOT NULL,
	provider_id VARCHAR DEFAULT '' NOT NULL,
	provider_model VARCHAR DEFAULT '' NOT NULL,
	prompt_version VARCHAR DEFAULT '' NOT NULL,
	schema_version INTEGER DEFAULT '0' NOT NULL,
	evidence_ids_json TEXT DEFAULT '[]' NOT NULL,
	output_json TEXT DEFAULT '{}' NOT NULL,
	token_input_count INTEGER DEFAULT '0' NOT NULL,
	token_output_count INTEGER DEFAULT '0' NOT NULL,
	latency_ms INTEGER DEFAULT '0' NOT NULL,
	retry_count INTEGER DEFAULT '0' NOT NULL,
	error_code VARCHAR DEFAULT '' NOT NULL,
	error_message TEXT DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(attempt_id) REFERENCES knowledge_brief_attempts (id) ON DELETE CASCADE
);
CREATE TABLE knowledge_brief_attempts (
	id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	snapshot_id INTEGER NOT NULL,
	status VARCHAR DEFAULT 'pending' NOT NULL,
	provider_id VARCHAR NOT NULL,
	provider_model VARCHAR NOT NULL,
	provider_base_url VARCHAR DEFAULT '' NOT NULL,
	context_window INTEGER NOT NULL,
	max_output_tokens INTEGER DEFAULT '0' NOT NULL,
	prompt_version VARCHAR NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	language VARCHAR DEFAULT 'zh-CN' NOT NULL,
	candidate_payload_json TEXT DEFAULT '' NOT NULL,
	validation_report_json TEXT DEFAULT '{}' NOT NULL,
	error_code VARCHAR DEFAULT '' NOT NULL,
	error_message TEXT DEFAULT '' NOT NULL,
	repair_count INTEGER DEFAULT '0' NOT NULL,
	fallback_provider_id VARCHAR DEFAULT '' NOT NULL,
	fallback_provider_model VARCHAR DEFAULT '' NOT NULL,
	actual_provider_id VARCHAR DEFAULT '' NOT NULL,
	actual_provider_model VARCHAR DEFAULT '' NOT NULL,
	provider_retry_count INTEGER DEFAULT '0' NOT NULL,
	next_retry_at DATETIME,
	token_input_count INTEGER DEFAULT '0' NOT NULL,
	token_output_count INTEGER DEFAULT '0' NOT NULL,
	latency_ms INTEGER DEFAULT '0' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE CASCADE,
	FOREIGN KEY(snapshot_id) REFERENCES knowledge_extraction_snapshots (id) ON DELETE CASCADE
);
CREATE TABLE knowledge_captured_source_metadata (
	source_id INTEGER NOT NULL,
	origin_note_id INTEGER NOT NULL,
	application_event_id INTEGER,
	note_fingerprint VARCHAR NOT NULL,
	selected_fragments_json TEXT NOT NULL,
	capture_schema_version VARCHAR NOT NULL,
	captured_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (source_id),
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE CASCADE
);
CREATE TABLE knowledge_evidence (
	id VARCHAR NOT NULL,
	source_id INTEGER NOT NULL,
	snapshot_id INTEGER NOT NULL,
	kind VARCHAR NOT NULL,
	block_kind VARCHAR NOT NULL,
	ordinal INTEGER NOT NULL,
	heading_path_json VARCHAR DEFAULT '[]' NOT NULL,
	char_start INTEGER NOT NULL,
	char_end INTEGER NOT NULL,
	line_start INTEGER NOT NULL,
	line_end INTEGER NOT NULL,
	canonical_excerpt TEXT NOT NULL,
	search_text VARCHAR DEFAULT '' NOT NULL,
	content_hash VARCHAR NOT NULL,
	asset_id INTEGER,
	previous_evidence_id VARCHAR,
	next_evidence_id VARCHAR,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_knowledge_evidence_snapshot_ordinal UNIQUE (snapshot_id, ordinal),
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE CASCADE,
	FOREIGN KEY(snapshot_id) REFERENCES knowledge_extraction_snapshots (id) ON DELETE CASCADE
);
PRAGMA writable_schema=ON;
INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql)VALUES('table','knowledge_evidence_fts','knowledge_evidence_fts',0,'CREATE VIRTUAL TABLE knowledge_evidence_fts USING fts5(
                        evidence_id UNINDEXED,
                        source_id UNINDEXED,
                        source_title,
                        heading_path,
                        content,
                        tokenize = ''trigram''
                    )');
CREATE TABLE 'knowledge_evidence_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
INSERT INTO "knowledge_evidence_fts_config" VALUES('version',4);
CREATE TABLE 'knowledge_evidence_fts_content'(id INTEGER PRIMARY KEY, c0, c1, c2, c3, c4);
CREATE TABLE 'knowledge_evidence_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
INSERT INTO "knowledge_evidence_fts_data" VALUES(1,X'');
INSERT INTO "knowledge_evidence_fts_data" VALUES(10,X'00000000000000');
CREATE TABLE 'knowledge_evidence_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE 'knowledge_evidence_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE knowledge_extraction_snapshots (
	id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	extractor_version VARCHAR NOT NULL,
	parser_version VARCHAR DEFAULT 'markdown-it-py-3' NOT NULL,
	normalization_version VARCHAR DEFAULT 'nl-1' NOT NULL,
	tokenizer_version VARCHAR DEFAULT 'none-1' NOT NULL,
	encoding VARCHAR DEFAULT 'utf-8' NOT NULL,
	detection_method VARCHAR DEFAULT '' NOT NULL,
	canonical_text TEXT NOT NULL,
	structure_manifest TEXT DEFAULT '{}' NOT NULL,
	metadata_extraction_version VARCHAR DEFAULT '' NOT NULL,
	digest VARCHAR NOT NULL,
	token_count INTEGER DEFAULT '0' NOT NULL,
	char_count INTEGER DEFAULT '0' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_knowledge_snapshots_source_version UNIQUE (source_id, extractor_version),
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE CASCADE
);
CREATE TABLE knowledge_jobs (
	id INTEGER NOT NULL,
	kind VARCHAR NOT NULL,
	queue VARCHAR NOT NULL,
	source_id INTEGER,
	attempt_id INTEGER,
	snapshot_id INTEGER,
	stage VARCHAR DEFAULT '' NOT NULL,
	status VARCHAR DEFAULT 'pending' NOT NULL,
	progress INTEGER DEFAULT '0' NOT NULL,
	retry_count INTEGER DEFAULT '0' NOT NULL,
	next_retry_at DATETIME,
	canceled BOOLEAN DEFAULT '0' NOT NULL,
	lease_owner VARCHAR DEFAULT '' NOT NULL,
	lease_expires_at DATETIME,
	heartbeat_at DATETIME,
	attempt_token VARCHAR DEFAULT '' NOT NULL,
	error_code VARCHAR DEFAULT '' NOT NULL,
	error_message VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE CASCADE,
	FOREIGN KEY(attempt_id) REFERENCES knowledge_brief_attempts (id) ON DELETE CASCADE
);
CREATE TABLE knowledge_logs (
	id INTEGER NOT NULL,
	source_id INTEGER,
	action VARCHAR NOT NULL,
	result VARCHAR DEFAULT 'succeeded' NOT NULL,
	error_code VARCHAR DEFAULT '' NOT NULL,
	occurred_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE knowledge_note_evidence (
	note_version_id INTEGER NOT NULL,
	block_id VARCHAR NOT NULL,
	evidence_id VARCHAR NOT NULL,
	PRIMARY KEY (note_version_id, block_id, evidence_id),
	CONSTRAINT uq_knowledge_note_block_evidence UNIQUE (note_version_id, block_id, evidence_id),
	FOREIGN KEY(note_version_id) REFERENCES knowledge_note_versions (id) ON DELETE RESTRICT,
	FOREIGN KEY(evidence_id) REFERENCES knowledge_evidence (id) ON DELETE RESTRICT
);
CREATE TABLE knowledge_note_versions (
	id INTEGER NOT NULL,
	note_id INTEGER NOT NULL,
	version_number INTEGER NOT NULL,
	content_json TEXT NOT NULL,
	content_hash VARCHAR NOT NULL,
	content_origin VARCHAR NOT NULL,
	capture_attempt_key VARCHAR NOT NULL,
	source_id INTEGER NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_knowledge_note_version_number UNIQUE (note_id, version_number),
	FOREIGN KEY(note_id) REFERENCES knowledge_notes (id) ON DELETE CASCADE,
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE RESTRICT
);
CREATE TABLE knowledge_notes (
	id INTEGER NOT NULL,
	title VARCHAR DEFAULT '' NOT NULL,
	current_version_id INTEGER,
	origin_kind VARCHAR NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	archived_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(current_version_id) REFERENCES knowledge_note_versions (id) ON DELETE SET NULL
);
CREATE TABLE knowledge_retrieval_traces (
	id INTEGER NOT NULL,
	"query" TEXT NOT NULL,
	filters_json TEXT DEFAULT '{}' NOT NULL,
	hits_json TEXT DEFAULT '[]' NOT NULL,
	duration_ms INTEGER DEFAULT '0' NOT NULL,
	evaluation_label VARCHAR DEFAULT '' NOT NULL,
	error_code VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE knowledge_source_assets (
	id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	logical_name VARCHAR NOT NULL,
	media_type VARCHAR NOT NULL,
	relative_path VARCHAR NOT NULL,
	bytes INTEGER NOT NULL,
	sha256 VARCHAR NOT NULL,
	width INTEGER DEFAULT '0' NOT NULL,
	height INTEGER DEFAULT '0' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_knowledge_source_assets_source_logical_name UNIQUE (source_id, logical_name),
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE CASCADE
);
CREATE TABLE knowledge_source_briefs (
	id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	snapshot_id INTEGER NOT NULL,
	winning_attempt_id INTEGER NOT NULL,
	schema_version INTEGER DEFAULT '1' NOT NULL,
	language VARCHAR DEFAULT 'zh-CN' NOT NULL,
	payload_json TEXT NOT NULL,
	outdated BOOLEAN DEFAULT '0' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_id),
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE CASCADE,
	FOREIGN KEY(snapshot_id) REFERENCES knowledge_extraction_snapshots (id) ON DELETE CASCADE
);
CREATE TABLE knowledge_source_origins (
	id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	import_method VARCHAR NOT NULL,
	original_filename VARCHAR DEFAULT '' NOT NULL,
	origin_url VARCHAR DEFAULT '' NOT NULL,
	imported_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE CASCADE
);
CREATE TABLE knowledge_sources (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
	source_hash VARCHAR NOT NULL,
	source_kind VARCHAR DEFAULT 'markdown' NOT NULL,
	display_title VARCHAR DEFAULT '' NOT NULL,
	title_hint VARCHAR DEFAULT '' NOT NULL,
	author VARCHAR DEFAULT '' NOT NULL,
	published_at DATETIME,
	main_filename VARCHAR NOT NULL,
	main_media_type VARCHAR DEFAULT 'text/markdown' NOT NULL,
	main_relative_path VARCHAR NOT NULL,
	manifest_json TEXT DEFAULT '{}' NOT NULL,
	total_bytes INTEGER NOT NULL,
	token_count INTEGER DEFAULT '0' NOT NULL,
	lifecycle VARCHAR DEFAULT 'active' NOT NULL,
	extraction_status VARCHAR DEFAULT 'pending' NOT NULL,
	extraction_error_code VARCHAR DEFAULT '' NOT NULL,
	extraction_error_message VARCHAR DEFAULT '' NOT NULL,
	brief_status VARCHAR DEFAULT 'not_started' NOT NULL,
	brief_block_reason VARCHAR DEFAULT '' NOT NULL,
	brief_error_code VARCHAR DEFAULT '' NOT NULL,
	brief_error_message VARCHAR DEFAULT '' NOT NULL,
	active_snapshot_id INTEGER,
	active_brief_id INTEGER,
	archived_at DATETIME,
	deleted_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	UNIQUE (source_hash)
);
CREATE TABLE material_revision_proposals (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	material_kit_id INTEGER NOT NULL,
	source_resume_id INTEGER,
	source_fingerprint_sha256 VARCHAR NOT NULL,
	source_snapshot_json VARCHAR NOT NULL,
	jd_version_id INTEGER,
	proposal_json VARCHAR NOT NULL,
	proposal_sha256 VARCHAR NOT NULL,
	status VARCHAR DEFAULT 'draft' NOT NULL,
	accepted_change_ids_json VARCHAR DEFAULT '[]' NOT NULL,
	result_resume_id INTEGER,
	accepted_at DATETIME,
	rejected_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_material_revision_proposals_result_resume UNIQUE (result_resume_id),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE,
	FOREIGN KEY(material_kit_id) REFERENCES application_material_kits (id) ON DELETE CASCADE,
	FOREIGN KEY(source_resume_id) REFERENCES resumes (id) ON DELETE SET NULL,
	FOREIGN KEY(result_resume_id) REFERENCES resumes (id) ON DELETE SET NULL
);
CREATE TABLE mock_interview_attempts (
	id INTEGER NOT NULL,
	context_kind VARCHAR DEFAULT 'application_event' NOT NULL,
	application_id INTEGER,
	event_id INTEGER,
	practice_case_id INTEGER,
	resume_id INTEGER NOT NULL,
	jd_version_id INTEGER,
	idempotency_key VARCHAR NOT NULL,
	input_snapshot_json TEXT NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	attempt_status VARCHAR NOT NULL,
	generation_revision INTEGER DEFAULT '1' NOT NULL,
	provider_call_token VARCHAR DEFAULT '' NOT NULL,
	provider_lease_until DATETIME,
	current_turn_no INTEGER DEFAULT '0' NOT NULL,
	transcript_fingerprint VARCHAR NOT NULL,
	failure_category VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	completed_at DATETIME,
	cancelled_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_mock_interview_attempts_context_key UNIQUE (context_kind, application_id, event_id, practice_case_id, idempotency_key),
	CONSTRAINT ck_mock_interview_attempt_context CHECK ((context_kind = 'application_event' AND application_id IS NOT NULL AND event_id IS NOT NULL AND practice_case_id IS NULL) OR (context_kind = 'quick_practice' AND application_id IS NULL AND event_id IS NULL AND practice_case_id IS NOT NULL)),
	FOREIGN KEY(practice_case_id) REFERENCES interview_practice_cases (id) ON DELETE RESTRICT
);
CREATE TABLE mock_interview_feedback_proposals (
	id INTEGER NOT NULL,
	attempt_id INTEGER NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	input_snapshot_json TEXT NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	transcript_fingerprint VARCHAR NOT NULL,
	proposal_json TEXT NOT NULL,
	proposal_hash VARCHAR NOT NULL,
	proposal_status VARCHAR NOT NULL,
	failure_category VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (attempt_id, idempotency_key)
);
CREATE TABLE mock_interview_review_drafts (
	id INTEGER NOT NULL,
	attempt_id INTEGER NOT NULL,
	proposal_id INTEGER NOT NULL,
	confirmation_idempotency_key VARCHAR NOT NULL,
	application_id INTEGER NOT NULL,
	event_id INTEGER NOT NULL,
	selected_blocks_json TEXT NOT NULL,
	content_hash VARCHAR NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	status VARCHAR DEFAULT 'confirmed' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (proposal_id)
);
CREATE TABLE mock_interview_turns (
	id INTEGER NOT NULL,
	attempt_id INTEGER NOT NULL,
	turn_no INTEGER NOT NULL,
	question_idempotency_key VARCHAR NOT NULL,
	turn_idempotency_key VARCHAR DEFAULT '' NOT NULL,
	question_text TEXT DEFAULT '' NOT NULL,
	answer_text TEXT DEFAULT '' NOT NULL,
	question_source_snapshot_json TEXT DEFAULT '{}' NOT NULL,
	answer_sha256 VARCHAR DEFAULT '' NOT NULL,
	turn_status VARCHAR NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (attempt_id, turn_no),
	UNIQUE (attempt_id, turn_no, turn_idempotency_key),
	UNIQUE (attempt_id, turn_no, question_idempotency_key)
);
CREATE TABLE offer_comparison_dimensions (
	id INTEGER NOT NULL,
	label VARCHAR NOT NULL,
	archived_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE offer_comparison_values (
	id INTEGER NOT NULL,
	offer_id INTEGER NOT NULL,
	dimension_id INTEGER NOT NULL,
	value_text VARCHAR,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_offer_comparison_values_offer_dimension UNIQUE (offer_id, dimension_id),
	FOREIGN KEY(offer_id) REFERENCES offers (id) ON DELETE CASCADE,
	FOREIGN KEY(dimension_id) REFERENCES offer_comparison_dimensions (id) ON DELETE CASCADE
);
CREATE TABLE offer_negotiation_briefs (
	id INTEGER NOT NULL,
	proposal_id INTEGER NOT NULL,
	offer_id INTEGER NOT NULL,
	origin_application_id INTEGER,
	confirmation_key VARCHAR DEFAULT '' NOT NULL,
	selected_blocks_json TEXT NOT NULL,
	edited_content_json TEXT NOT NULL,
	content_hash VARCHAR DEFAULT '' NOT NULL,
	confirmed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_offer_negotiation_briefs_proposal UNIQUE (proposal_id)
);
CREATE TABLE offer_negotiation_proposals (
	id INTEGER NOT NULL,
	offer_id INTEGER NOT NULL,
	application_id INTEGER,
	idempotency_key VARCHAR NOT NULL,
	attempt_status VARCHAR DEFAULT 'generating' NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	input_snapshot_json TEXT NOT NULL,
	proposal_json TEXT,
	proposal_hash VARCHAR,
	source_states_json TEXT DEFAULT '{}' NOT NULL,
	provider_call_token VARCHAR DEFAULT '' NOT NULL,
	lease_expires_at DATETIME,
	revision INTEGER DEFAULT '1' NOT NULL,
	invalidation_reason VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	ready_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_offer_negotiation_proposals_offer_key UNIQUE (offer_id, idempotency_key)
);
CREATE TABLE offers (
	id INTEGER NOT NULL,
	application_id INTEGER,
	company_name VARCHAR NOT NULL,
	position_name VARCHAR NOT NULL,
	status VARCHAR DEFAULT 'pending' NOT NULL,
	base_monthly INTEGER DEFAULT '0' NOT NULL,
	months_per_year INTEGER DEFAULT '12' NOT NULL,
	signing_bonus INTEGER DEFAULT '0' NOT NULL,
	equity VARCHAR DEFAULT '' NOT NULL,
	perks VARCHAR DEFAULT '' NOT NULL,
	deadline VARCHAR DEFAULT '' NOT NULL,
	notes VARCHAR DEFAULT '' NOT NULL,
	assessment VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE SET NULL
);
CREATE TABLE opportunity_fit_review_sessions (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	triage_idempotency_key VARCHAR NOT NULL,
	proposal_schema_version INTEGER DEFAULT '2' NOT NULL,
	status VARCHAR DEFAULT 'active' NOT NULL,
	jd_version_id INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_opportunity_fit_sessions_application_triage_key UNIQUE (application_id, triage_idempotency_key),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE
);
CREATE TABLE opportunity_fit_review_stages (
	id INTEGER NOT NULL,
	review_id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	resume_id INTEGER,
	parent_triage_stage_id INTEGER,
	stage VARCHAR NOT NULL,
	proposal_schema_version INTEGER DEFAULT '2' NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	source_snapshot_json VARCHAR NOT NULL,
	source_fingerprint_sha256 VARCHAR NOT NULL,
	proposal_json VARCHAR NOT NULL,
	jd_version_id INTEGER,
	proposal_sha256 VARCHAR NOT NULL,
	status VARCHAR DEFAULT 'ready' NOT NULL,
	stage_generation INTEGER DEFAULT '1' NOT NULL,
	provider_call_token VARCHAR DEFAULT '' NOT NULL,
	lease_expires_at DATETIME,
	confirmation_token_hash VARCHAR DEFAULT '' NOT NULL,
	confirmation_expires_at DATETIME,
	confirmed_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_opportunity_fit_stages_application_stage_key UNIQUE (application_id, stage, idempotency_key),
	FOREIGN KEY(review_id) REFERENCES opportunity_fit_review_sessions (id) ON DELETE CASCADE,
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE,
	FOREIGN KEY(resume_id) REFERENCES resumes (id) ON DELETE SET NULL,
	FOREIGN KEY(parent_triage_stage_id) REFERENCES opportunity_fit_review_stages (id) ON DELETE RESTRICT
);
CREATE TABLE opportunity_fit_reviews (
	id INTEGER NOT NULL,
	application_id INTEGER NOT NULL,
	resume_id INTEGER,
	idempotency_key VARCHAR NOT NULL,
	proposal_schema_version INTEGER DEFAULT '1' NOT NULL,
	source_fingerprint_sha256 VARCHAR NOT NULL,
	source_snapshot_json VARCHAR NOT NULL,
	triage_json VARCHAR NOT NULL,
	triage_sha256 VARCHAR NOT NULL,
	deep_review_json VARCHAR,
	deep_review_sha256 VARCHAR,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	deep_reviewed_at DATETIME, jd_version_id INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT uq_opportunity_fit_reviews_application_idempotency UNIQUE (application_id, idempotency_key),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE CASCADE,
	FOREIGN KEY(resume_id) REFERENCES resumes (id) ON DELETE SET NULL
);
CREATE TABLE question_reviews (
	id INTEGER NOT NULL,
	question_id INTEGER NOT NULL,
	rating INTEGER NOT NULL,
	note VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(question_id) REFERENCES questions (id) ON DELETE CASCADE
);
CREATE TABLE questions (
	id INTEGER NOT NULL,
	application_id INTEGER,
	topic VARCHAR DEFAULT '' NOT NULL,
	category VARCHAR DEFAULT '' NOT NULL,
	difficulty VARCHAR DEFAULT 'medium' NOT NULL,
	question VARCHAR NOT NULL,
	reference_answer VARCHAR DEFAULT '' NOT NULL,
	tags VARCHAR DEFAULT '[]' NOT NULL,
	source_type VARCHAR DEFAULT 'manual' NOT NULL,
	status VARCHAR DEFAULT 'new' NOT NULL,
	practice_count INTEGER DEFAULT '0' NOT NULL,
	last_practiced_at DATETIME,
	next_review_at DATETIME,
	question_hash VARCHAR DEFAULT '' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE SET NULL
);
CREATE TABLE resume_matches (
	id INTEGER NOT NULL,
	resume_id INTEGER NOT NULL,
	application_id INTEGER,
	jd_text VARCHAR NOT NULL,
	result VARCHAR NOT NULL,
	jd_version_id INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(resume_id) REFERENCES resumes (id) ON DELETE CASCADE,
	FOREIGN KEY(application_id) REFERENCES applications (id) ON DELETE SET NULL
);
CREATE TABLE resumes (
	id INTEGER NOT NULL,
	name VARCHAR DEFAULT '' NOT NULL,
	file_path VARCHAR DEFAULT '' NOT NULL,
	parsed_data VARCHAR DEFAULT '' NOT NULL,
	parse_status VARCHAR DEFAULT 'pending' NOT NULL,
	title VARCHAR DEFAULT '' NOT NULL,
	is_master BOOLEAN DEFAULT '0' NOT NULL,
	parent_resume_id INTEGER,
	source VARCHAR DEFAULT 'manual' NOT NULL,
	source_file_path VARCHAR DEFAULT '' NOT NULL,
	content_json VARCHAR DEFAULT '{}' NOT NULL,
	deleted_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(parent_resume_id) REFERENCES resumes (id) ON DELETE SET NULL
);
CREATE TABLE schema_migrations (
                    version TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
INSERT INTO "schema_migrations" VALUES('knowledge_rewrite_reset','Knowledge rewrite base schema applied','2026-08-30 09:31:49');
INSERT INTO "schema_migrations" VALUES('0026_write_operation_ledger','Add durable Agent write operation ledger and fenced delivery identity','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0025_pending_confirmation_claim','Add private Pending Action confirmation claim identity and lease','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0024_durable_execution_journal','Add fail-open durable Agent Run journal tables','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0027_context_projector_manifest_v2','Allow privacy-bounded Context Projector manifests up to 64 KiB','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0016_event_bound_mock_interview','Replace legacy MockSession with event-bound text mock interview tables','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0018_application_jd_versions','Add immutable Application JD version history and identity columns','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0019_interview_story_library','Add versioned interview stories with evidence-gated proposal attempts','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0020_application_outcome_feedback','Add frozen application submission snapshots and append-only outcome feedback','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0021_adaptive_interview_practice','Add evidence-backed adaptive interview practice plans','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0022_voice_coaching_snapshots','Add immutable user-confirmed local voice coaching snapshots','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0023_immersive_interview_studio','Add immutable quick-practice cases and dual interview contexts','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0017_offer_comparison_negotiation','Add Offer comparison dimensions, values, negotiation proposals and briefs','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0014_opportunity_fit_v1_schema_marker','Mark legacy opportunity fit reviews as schema version 1','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0010_interview_review_proposals','Add event-bound interview review proposals','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0011_confirmed_interview_knowledge_capture','Add confirmed interview knowledge capture','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0012_interview_preparation_proposals','Add evidence-gated interview preparation proposals','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0013_opportunity_fit_v2','Add neutral two-stage opportunity fit review sessions and stages','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0001_base_schema','Create current application tables','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0028_scoped_tool_authority','Add Conversation scope revision and Write Operation authorization fingerprint','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0006_application_evidence_bundles','Add immutable application evidence bundles','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0007_material_revision_proposals','Add evidence-gated material revision proposals','2026-08-30 09:31:50');
INSERT INTO "schema_migrations" VALUES('0008_opportunity_fit_reviews','Add immutable opportunity fit reviews','2026-08-30 09:31:50');
CREATE TABLE voice_coaching_snapshots (
	id INTEGER NOT NULL,
	attempt_id INTEGER NOT NULL,
	turn_id INTEGER NOT NULL,
	context_kind VARCHAR DEFAULT 'application_event' NOT NULL,
	application_id INTEGER,
	event_id INTEGER,
	practice_case_id INTEGER,
	idempotency_key VARCHAR NOT NULL,
	request_fingerprint_sha256 VARCHAR NOT NULL,
	question_text_snapshot TEXT NOT NULL,
	confirmed_answer_text_snapshot TEXT NOT NULL,
	answer_sha256 VARCHAR NOT NULL,
	measurement_source VARCHAR DEFAULT 'local_browser_measurement' NOT NULL,
	total_duration_ms INTEGER NOT NULL,
	voiced_duration_ms INTEGER NOT NULL,
	pause_count INTEGER NOT NULL,
	longest_pause_ms INTEGER NOT NULL,
	speech_rate_cpm INTEGER,
	filler_occurrences_json TEXT DEFAULT '[]' NOT NULL,
	reflection_text TEXT DEFAULT '' NOT NULL,
	focus_kind VARCHAR,
	origin_snapshot_id INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_voice_coaching_snapshots_turn UNIQUE (turn_id),
	CONSTRAINT uq_voice_coaching_snapshots_key UNIQUE (idempotency_key),
	CONSTRAINT ck_voice_coaching_snapshot_context CHECK ((context_kind = 'application_event' AND application_id IS NOT NULL AND event_id IS NOT NULL AND practice_case_id IS NULL) OR (context_kind = 'quick_practice' AND application_id IS NULL AND event_id IS NULL AND practice_case_id IS NOT NULL)),
	FOREIGN KEY(attempt_id) REFERENCES mock_interview_attempts (id) ON DELETE CASCADE,
	FOREIGN KEY(turn_id) REFERENCES mock_interview_turns (id) ON DELETE CASCADE,
	FOREIGN KEY(practice_case_id) REFERENCES interview_practice_cases (id) ON DELETE RESTRICT,
	FOREIGN KEY(origin_snapshot_id) REFERENCES voice_coaching_snapshots (id) ON DELETE SET NULL
);
CREATE TABLE wakeups (
	id INTEGER NOT NULL,
	kind VARCHAR NOT NULL,
	due_at DATETIME NOT NULL,
	payload_json VARCHAR DEFAULT '{}' NOT NULL,
	status VARCHAR DEFAULT 'pending' NOT NULL,
	dispatched_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE write_operation_transitions (
	id VARCHAR(36) NOT NULL,
	operation_id VARCHAR(36) NOT NULL,
	seq INTEGER NOT NULL,
	state VARCHAR NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_write_operation_transitions_id_uuid CHECK (length(id) = 36 AND lower(id) = id AND substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-' AND length(replace(id, '-', '')) = 32 AND id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_write_operation_transitions_state CHECK (state IN ('proposed','approved','rejected','claimed','committed','failed')),
	CONSTRAINT ck_write_operation_transitions_seq CHECK (seq >= 1),
	CONSTRAINT uq_write_operation_transitions_seq UNIQUE (operation_id, seq),
	FOREIGN KEY(operation_id) REFERENCES write_operations (id) ON DELETE CASCADE
);
CREATE TABLE write_operations (
	id VARCHAR(36) NOT NULL,
	operation_role VARCHAR NOT NULL,
	parent_operation_id VARCHAR(36),
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
	delivery_next_operation_id VARCHAR(36),
	delivery_generation INTEGER NOT NULL,
	delivery_owner_token_fingerprint VARCHAR,
	delivery_lease_expires_at INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	approved_at DATETIME,
	claimed_at DATETIME,
	rejected_at DATETIME,
	committed_at DATETIME,
	failed_at DATETIME,
	delivered_at DATETIME,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_write_operations_id_uuid CHECK (length(id) = 36 AND lower(id) = id AND substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-' AND length(replace(id, '-', '')) = 32 AND id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_write_operations_role CHECK (operation_role IN ('primary','compensation')),
	CONSTRAINT ck_write_operations_adapter CHECK (adapter_kind IN ('typed','legacy_deterministic','compensation')),
	CONSTRAINT ck_write_operations_status CHECK (status IN ('proposed','rejected','committed','failed')),
	CONSTRAINT ck_write_operations_delivery_status CHECK (delivery_status IN ('pending','completed','failed','not_applicable')),
	CONSTRAINT ck_write_operations_role_identity CHECK ((operation_role = 'primary' AND parent_operation_id IS NULL AND parent_terminal_payload_sha256 IS NULL AND tool_call_id IS NOT NULL AND tool_call_id <> '' AND proposal_fingerprint IS NOT NULL AND confirmation_token_fingerprint IS NOT NULL) OR (operation_role = 'compensation' AND parent_operation_id IS NOT NULL AND parent_terminal_payload_sha256 IS NOT NULL AND tool_call_id IS NULL AND proposal_fingerprint IS NULL AND confirmation_token_fingerprint IS NULL)),
	CONSTRAINT ck_write_operations_manifest CHECK ((operation_role = 'primary' AND adapter_kind = 'typed' AND tool_name IN ('create_application','update_application_status','create_application_event','update_application_event','delete_application_event','add_note','update_note','delete_note','update_offer','save_offer_assessment','resume_update_career_intent','resume_rewrite_highlight')) OR (operation_role = 'primary' AND adapter_kind = 'legacy_deterministic' AND tool_name IN ('save_application_jd_version','create_application_submission_snapshot','record_application_outcome')) OR (operation_role = 'compensation' AND adapter_kind = 'compensation' AND tool_name IN ('undo:update_application_status','undo:create_application','undo:create_application_event','undo:add_note'))),
	CONSTRAINT ck_write_operations_result_bytes CHECK (result_json IS NULL OR length(CAST(result_json AS BLOB)) <= 524288),
	CONSTRAINT ck_write_operations_visible_bytes CHECK (visible_result IS NULL OR length(CAST(visible_result AS BLOB)) <= 262144),
	CONSTRAINT ck_write_operations_transport_bytes CHECK (transport_json IS NULL OR length(CAST(transport_json AS BLOB)) <= 131072),
	CONSTRAINT ck_write_operations_undo_bytes CHECK (undo_json IS NULL OR length(CAST(undo_json AS BLOB)) <= 65536),
	CONSTRAINT ck_write_operations_terminal_bytes CHECK (coalesce(length(CAST(result_json AS BLOB)),0) + coalesce(length(CAST(visible_result AS BLOB)),0) + coalesce(length(CAST(transport_json AS BLOB)),0) + coalesce(length(CAST(undo_json AS BLOB)),0) <= 1048576),
	CONSTRAINT ck_write_operations_failure_category CHECK (failure_category IS NULL OR failure_category IN ('validation_error','permission_denied','confirmation_rejected','stale_state','conflict','not_found','provider_error','internal_error')),
	CONSTRAINT ck_write_operations_failure_code CHECK (failure_code IS NULL OR (length(CAST(failure_code AS BLOB)) BETWEEN 1 AND 128 AND failure_code NOT GLOB '*[^ -~]*')),
	CONSTRAINT ck_write_operations_key_id CHECK (length(fingerprint_key_id) = 36 AND lower(fingerprint_key_id) = fingerprint_key_id AND substr(fingerprint_key_id,9,1) = '-' AND substr(fingerprint_key_id,14,1) = '-' AND substr(fingerprint_key_id,19,1) = '-' AND substr(fingerprint_key_id,24,1) = '-' AND fingerprint_key_id NOT GLOB '*[^0-9a-f-]*'),
	CONSTRAINT ck_write_operations_hmac_fingerprints CHECK ((proposal_fingerprint IS NULL OR (length(proposal_fingerprint) = 76 AND substr(proposal_fingerprint,1,12) = 'hmac-sha256:' AND substr(proposal_fingerprint,13) NOT GLOB '*[^0-9a-f]*')) AND (input_fingerprint IS NULL OR (length(input_fingerprint) = 76 AND substr(input_fingerprint,1,12) = 'hmac-sha256:' AND substr(input_fingerprint,13) NOT GLOB '*[^0-9a-f]*')) AND (confirmation_token_fingerprint IS NULL OR (length(confirmation_token_fingerprint) = 76 AND substr(confirmation_token_fingerprint,1,12) = 'hmac-sha256:' AND substr(confirmation_token_fingerprint,13) NOT GLOB '*[^0-9a-f]*')) AND (operation_request_fingerprint IS NULL OR (length(operation_request_fingerprint) = 76 AND substr(operation_request_fingerprint,1,12) = 'hmac-sha256:' AND substr(operation_request_fingerprint,13) NOT GLOB '*[^0-9a-f]*')) AND (delivery_owner_token_fingerprint IS NULL OR (length(delivery_owner_token_fingerprint) = 76 AND substr(delivery_owner_token_fingerprint,1,12) = 'hmac-sha256:' AND substr(delivery_owner_token_fingerprint,13) NOT GLOB '*[^0-9a-f]*'))),
	CONSTRAINT ck_write_operations_authorization_scope_fingerprint CHECK (authorization_scope_fingerprint IS NULL OR (length(authorization_scope_fingerprint) = 76 AND substr(authorization_scope_fingerprint,1,12) = 'hmac-sha256:' AND substr(authorization_scope_fingerprint,13) NOT GLOB '*[^0-9a-f]*')),
	CONSTRAINT ck_write_operations_typed_primary_scope_bound CHECK (NOT (operation_role = 'primary' AND adapter_kind = 'typed' AND status = 'proposed' AND authorization_scope_fingerprint IS NULL)),
	CONSTRAINT ck_write_operations_sha256_digests CHECK ((parent_terminal_payload_sha256 IS NULL OR (length(parent_terminal_payload_sha256) = 71 AND substr(parent_terminal_payload_sha256,1,7) = 'sha256:' AND substr(parent_terminal_payload_sha256,8) NOT GLOB '*[^0-9a-f]*')) AND (terminal_payload_sha256 IS NULL OR (length(terminal_payload_sha256) = 71 AND substr(terminal_payload_sha256,1,7) = 'sha256:' AND substr(terminal_payload_sha256,8) NOT GLOB '*[^0-9a-f]*')) AND (delivery_manifest_sha256 IS NULL OR (length(delivery_manifest_sha256) = 71 AND substr(delivery_manifest_sha256,1,7) = 'sha256:' AND substr(delivery_manifest_sha256,8) NOT GLOB '*[^0-9a-f]*'))),
	CONSTRAINT ck_write_operations_terminal_shape CHECK ((status = 'proposed' AND input_fingerprint IS NULL AND ((operation_role = 'primary' AND operation_request_fingerprint IS NULL) OR (operation_role = 'compensation' AND operation_request_fingerprint IS NOT NULL)) AND result_contract IS NULL AND result_json IS NULL AND visible_result IS NULL AND transport_json IS NULL AND undo_json IS NULL AND terminal_payload_sha256 IS NULL AND failure_category IS NULL AND failure_code IS NULL AND approved_at IS NULL AND claimed_at IS NULL AND rejected_at IS NULL AND committed_at IS NULL AND failed_at IS NULL) OR (status = 'rejected' AND operation_role = 'primary' AND result_contract = 'rejection_json_v1' AND operation_request_fingerprint IS NOT NULL AND input_fingerprint IS NULL AND result_json IS NOT NULL AND visible_result IS NOT NULL AND transport_json IS NOT NULL AND undo_json IS NULL AND terminal_payload_sha256 IS NOT NULL AND failure_category IS NULL AND failure_code IS NULL AND approved_at IS NULL AND claimed_at IS NULL AND rejected_at IS NOT NULL AND committed_at IS NULL AND failed_at IS NULL) OR (status = 'committed' AND operation_request_fingerprint IS NOT NULL AND input_fingerprint IS NOT NULL AND result_json IS NOT NULL AND visible_result IS NOT NULL AND transport_json IS NOT NULL AND terminal_payload_sha256 IS NOT NULL AND failure_category IS NULL AND failure_code IS NULL AND approved_at IS NOT NULL AND claimed_at IS NOT NULL AND rejected_at IS NULL AND committed_at IS NOT NULL AND failed_at IS NULL) OR (status = 'failed' AND operation_request_fingerprint IS NOT NULL AND input_fingerprint IS NOT NULL AND result_json IS NOT NULL AND visible_result IS NOT NULL AND transport_json IS NOT NULL AND undo_json IS NULL AND terminal_payload_sha256 IS NOT NULL AND failure_category IS NOT NULL AND failure_code IS NOT NULL AND approved_at IS NOT NULL AND claimed_at IS NOT NULL AND rejected_at IS NULL AND committed_at IS NULL AND failed_at IS NOT NULL)),
	CONSTRAINT ck_write_operations_result_contract CHECK (status NOT IN ('committed','failed') OR (adapter_kind = 'typed' AND result_contract = 'typed_json_v1') OR (adapter_kind = 'legacy_deterministic' AND result_contract = 'legacy_string_v1') OR (adapter_kind = 'compensation' AND result_contract = 'compensation_json_v1')),
	CONSTRAINT ck_write_operations_undo_policy CHECK (status <> 'committed' OR (operation_role = 'primary' AND tool_name IN ('create_application','update_application_status','create_application_event','add_note') AND undo_json IS NOT NULL) OR ((operation_role = 'compensation' OR tool_name NOT IN ('create_application','update_application_status','create_application_event','add_note')) AND undo_json IS NULL)),
	CONSTRAINT ck_write_operations_delivery_shape CHECK ((status = 'proposed' AND delivery_status = 'pending' AND delivery_generation = 0 AND delivery_owner_token_fingerprint IS NULL AND delivery_lease_expires_at IS NULL AND delivery_outcome IS NULL AND delivery_message_count IS NULL AND delivery_manifest_sha256 IS NULL AND delivery_next_operation_id IS NULL AND delivered_at IS NULL AND delivery_failure_code IS NULL) OR (status <> 'proposed' AND operation_role = 'primary' AND delivery_status = 'pending' AND delivery_generation >= 1 AND delivery_owner_token_fingerprint IS NOT NULL AND delivery_lease_expires_at IS NOT NULL AND delivery_outcome IS NULL AND delivery_message_count IS NULL AND delivery_manifest_sha256 IS NULL AND delivery_next_operation_id IS NULL AND delivered_at IS NULL AND delivery_failure_code IS NULL) OR (status <> 'proposed' AND operation_role = 'primary' AND delivery_status = 'completed' AND delivery_generation >= 1 AND delivery_owner_token_fingerprint IS NULL AND delivery_lease_expires_at IS NULL AND delivery_outcome IN ('final_response','chained_pending') AND delivery_message_count >= 2 AND delivery_manifest_sha256 IS NOT NULL AND delivered_at IS NOT NULL AND delivery_failure_code IS NULL AND ((delivery_outcome = 'chained_pending' AND delivery_next_operation_id IS NOT NULL) OR (delivery_outcome = 'final_response' AND delivery_next_operation_id IS NULL))) OR (status <> 'proposed' AND operation_role = 'primary' AND delivery_status = 'failed' AND delivery_generation >= 1 AND delivery_owner_token_fingerprint IS NULL AND delivery_lease_expires_at IS NULL AND delivery_outcome = 'fallback' AND delivery_message_count = 2 AND delivery_manifest_sha256 IS NOT NULL AND delivery_next_operation_id IS NULL AND delivered_at IS NOT NULL AND delivery_failure_code IS NOT NULL) OR (status <> 'proposed' AND operation_role = 'compensation' AND delivery_status = 'not_applicable' AND delivery_generation = 0 AND delivery_outcome = 'none' AND delivery_message_count = 0 AND delivery_owner_token_fingerprint IS NULL AND delivery_lease_expires_at IS NULL AND delivery_manifest_sha256 IS NULL AND delivery_next_operation_id IS NULL AND delivery_failure_code IS NULL AND delivered_at IS NOT NULL AND ((status = 'committed' AND delivered_at = committed_at) OR (status = 'failed' AND delivered_at = failed_at)))),
	FOREIGN KEY(parent_operation_id) REFERENCES write_operations (id) ON DELETE RESTRICT,
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE SET NULL,
	FOREIGN KEY(delivery_next_operation_id) REFERENCES write_operations (id) ON DELETE RESTRICT
);
CREATE INDEX idx_applications_status ON applications (status);
CREATE INDEX idx_offer_comparison_dimensions_active ON offer_comparison_dimensions (archived_at);
CREATE INDEX idx_offer_negotiation_proposals_offer ON offer_negotiation_proposals (offer_id);
CREATE INDEX idx_offer_negotiation_briefs_offer ON offer_negotiation_briefs (offer_id);
CREATE INDEX idx_adaptive_practice_application ON adaptive_practice_plans (application_id, created_at);
CREATE INDEX idx_adaptive_practice_status ON adaptive_practice_plans (status, created_at);
CREATE INDEX idx_interview_preparation_resume ON interview_preparation_proposals (resume_id);
CREATE INDEX idx_interview_preparation_event ON interview_preparation_proposals (application_event_id);
CREATE INDEX idx_interview_preparation_application ON interview_preparation_proposals (application_id);
CREATE INDEX idx_knowledge_notes_origin ON knowledge_notes (origin_kind);
CREATE INDEX idx_knowledge_note_versions_note ON knowledge_note_versions (note_id);
CREATE INDEX idx_mock_interview_turns_attempt ON mock_interview_turns (attempt_id, turn_no);
CREATE INDEX idx_mock_interview_feedback_attempt ON mock_interview_feedback_proposals (attempt_id);
CREATE INDEX idx_mock_interview_review_drafts_attempt ON mock_interview_review_drafts (attempt_id);
CREATE INDEX idx_interview_stories_status ON interview_stories (status);
CREATE INDEX idx_interview_story_attempt_status ON interview_story_proposal_attempts (attempt_status);
CREATE INDEX idx_interview_story_attempt_target ON interview_story_proposal_attempts (target_story_id);
CREATE INDEX idx_wakeups_kind ON wakeups (kind);
CREATE INDEX idx_wakeups_status_due ON wakeups (status, due_at);
CREATE INDEX idx_knowledge_sources_hash ON knowledge_sources (source_hash);
CREATE INDEX idx_knowledge_sources_extraction ON knowledge_sources (extraction_status);
CREATE INDEX idx_knowledge_sources_lifecycle ON knowledge_sources (lifecycle);
CREATE INDEX idx_knowledge_logs_action ON knowledge_logs (action);
CREATE INDEX idx_knowledge_logs_source ON knowledge_logs (source_id);
CREATE INDEX idx_knowledge_retrieval_traces_created ON knowledge_retrieval_traces (created_at);
CREATE INDEX idx_knowledge_retrieval_traces_label ON knowledge_retrieval_traces (evaluation_label);
CREATE INDEX idx_application_jd_versions_app_version ON application_jd_versions (application_id, version_number);
CREATE INDEX idx_application_events_type ON application_events (event_type);
CREATE INDEX idx_application_events_app ON application_events (application_id);
CREATE INDEX idx_offers_app ON offers (application_id);
CREATE INDEX idx_offers_status ON offers (status);
CREATE INDEX idx_matches_resume ON resume_matches (resume_id);
CREATE INDEX idx_jd_app ON jd_analyses (application_id);
CREATE INDEX idx_evidence_bundles_application ON application_evidence_bundles (application_id);
CREATE INDEX idx_opportunity_fit_reviews_application_created ON opportunity_fit_reviews (application_id, created_at);
CREATE INDEX idx_opportunity_fit_sessions_application_created ON opportunity_fit_review_sessions (application_id, created_at);
CREATE INDEX idx_questions_hash ON questions (question_hash);
CREATE INDEX idx_questions_topic ON questions (topic);
CREATE INDEX idx_questions_status ON questions (status);
CREATE INDEX idx_questions_next_review ON questions (next_review_at);
CREATE INDEX idx_interview_practice_cases_status ON interview_practice_cases (status, id);
CREATE INDEX idx_interview_story_versions_story ON interview_story_versions (story_id, version_number);
CREATE UNIQUE INDEX uq_write_operations_primary_call ON write_operations (conversation_id, tool_call_id) WHERE operation_role = 'primary' AND conversation_id IS NOT NULL;
CREATE INDEX idx_write_operations_status ON write_operations (status, delivery_status);
CREATE UNIQUE INDEX uq_write_operations_compensation_parent ON write_operations (parent_operation_id) WHERE operation_role = 'compensation';
CREATE INDEX idx_knowledge_source_origins_source ON knowledge_source_origins (source_id);
CREATE INDEX idx_knowledge_snapshots_source ON knowledge_extraction_snapshots (source_id);
CREATE INDEX idx_knowledge_source_assets_source ON knowledge_source_assets (source_id);
CREATE INDEX idx_notes_app ON interview_notes (application_id);
CREATE INDEX idx_offer_comparison_values_offer ON offer_comparison_values (offer_id);
CREATE INDEX idx_material_kits_status ON application_material_kits (status);
CREATE INDEX idx_material_kits_app ON application_material_kits (application_id);
CREATE INDEX idx_opportunity_fit_stages_parent ON opportunity_fit_review_stages (parent_triage_stage_id);
CREATE INDEX idx_opportunity_fit_stages_review_created ON opportunity_fit_review_stages (review_id, created_at);
CREATE INDEX idx_question_reviews_question ON question_reviews (question_id);
CREATE INDEX idx_mock_interview_attempts_context ON mock_interview_attempts (context_kind, practice_case_id);
CREATE INDEX idx_mock_interview_attempts_event ON mock_interview_attempts (application_id, event_id);
CREATE INDEX idx_interview_story_evidence_version ON interview_story_version_evidence_links (story_version_id);
CREATE INDEX idx_interview_story_assertions_version ON interview_story_user_assertions (story_version_id);
CREATE INDEX idx_chat_messages_conv ON chat_messages (conversation_id);
CREATE UNIQUE INDEX uq_chat_messages_operation_ordinal ON chat_messages (operation_id, delivery_ordinal) WHERE operation_id IS NOT NULL;
CREATE INDEX idx_write_operation_transitions_operation ON write_operation_transitions (operation_id, seq);
CREATE INDEX idx_knowledge_evidence_source ON knowledge_evidence (source_id);
CREATE INDEX idx_knowledge_evidence_snapshot ON knowledge_evidence (snapshot_id);
CREATE INDEX idx_knowledge_source_briefs_source ON knowledge_source_briefs (source_id);
CREATE INDEX idx_knowledge_brief_attempts_source ON knowledge_brief_attempts (source_id);
CREATE INDEX idx_knowledge_brief_attempts_status ON knowledge_brief_attempts (status);
CREATE INDEX idx_application_submission_snapshots_app ON application_submission_snapshots (application_id, submitted_at);
CREATE INDEX idx_material_revision_proposals_application_created ON material_revision_proposals (application_id, created_at);
CREATE INDEX idx_interview_review_proposals_note ON interview_review_proposals (note_id);
CREATE INDEX idx_interview_capture_attempt_note ON interview_knowledge_capture_attempts (note_id);
CREATE INDEX idx_knowledge_note_evidence_version ON knowledge_note_evidence (note_version_id);
CREATE INDEX idx_voice_coaching_snapshots_created ON voice_coaching_snapshots (created_at, id);
CREATE INDEX idx_voice_coaching_snapshots_attempt ON voice_coaching_snapshots (attempt_id);
CREATE INDEX idx_voice_coaching_snapshots_context ON voice_coaching_snapshots (context_kind, practice_case_id);
CREATE INDEX idx_voice_coaching_snapshots_application_event ON voice_coaching_snapshots (application_id, event_id);
CREATE INDEX idx_agent_runs_conversation_waiting ON agent_runs (conversation_id, waiting_tool_call_id);
CREATE UNIQUE INDEX uq_agent_runs_waiting_tool_call ON agent_runs (conversation_id, waiting_tool_call_id) WHERE waiting_tool_call_id IS NOT NULL;
CREATE INDEX idx_knowledge_jobs_status ON knowledge_jobs (status);
CREATE INDEX idx_knowledge_jobs_source ON knowledge_jobs (source_id);
CREATE INDEX idx_knowledge_jobs_queue ON knowledge_jobs (queue);
CREATE INDEX idx_knowledge_jobs_attempt ON knowledge_jobs (attempt_id);
CREATE INDEX idx_knowledge_brief_attempt_steps_phase ON knowledge_brief_attempt_steps (phase);
CREATE INDEX idx_knowledge_brief_attempt_steps_attempt ON knowledge_brief_attempt_steps (attempt_id, sequence);
CREATE INDEX idx_application_outcomes_app_occurred ON application_outcomes (application_id, occurred_at);
CREATE INDEX idx_application_outcomes_snapshot ON application_outcomes (submission_snapshot_id);
CREATE INDEX idx_agent_events_segment ON agent_events (run_id, execution_segment_id, seq);
CREATE INDEX idx_agent_events_type ON agent_events (run_id, event_type, seq);
CREATE INDEX idx_agent_context_segment_step ON agent_context_snapshots (run_id, execution_segment_id, model_step);
CREATE TRIGGER trg_write_operation_compensation_insert
                BEFORE INSERT ON write_operations
                WHEN NEW.operation_role = 'compensation'
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM write_operations parent
                        WHERE parent.id = NEW.parent_operation_id
                          AND parent.operation_role = 'primary'
                          AND parent.status = 'committed'
                          AND parent.terminal_payload_sha256 = NEW.parent_terminal_payload_sha256
                          AND length(NEW.parent_terminal_payload_sha256) = 71
                          AND substr(NEW.parent_terminal_payload_sha256,1,7) = 'sha256:'
                          AND substr(NEW.parent_terminal_payload_sha256,8) NOT GLOB '*[^0-9a-f]*'
                    ) THEN RAISE(ABORT, 'invalid compensation parent') END;
                END;
CREATE TRIGGER trg_write_operation_terminal_immutable
                BEFORE UPDATE ON write_operations
                WHEN OLD.status <> 'proposed' AND (
                    NEW.status <> OLD.status OR
                    NEW.operation_role IS NOT OLD.operation_role OR
                    NEW.parent_operation_id IS NOT OLD.parent_operation_id OR
                    NEW.parent_terminal_payload_sha256 IS NOT OLD.parent_terminal_payload_sha256 OR
                    NEW.agent_run_id IS NOT OLD.agent_run_id OR
                    NEW.tool_call_id IS NOT OLD.tool_call_id OR
                    NEW.tool_name IS NOT OLD.tool_name OR
                    NEW.adapter_kind IS NOT OLD.adapter_kind OR
                    NEW.fingerprint_key_id IS NOT OLD.fingerprint_key_id OR
                    NEW.proposal_fingerprint IS NOT OLD.proposal_fingerprint OR
                    NEW.input_fingerprint IS NOT OLD.input_fingerprint OR
                    NEW.confirmation_token_fingerprint IS NOT OLD.confirmation_token_fingerprint OR
                    NEW.operation_request_fingerprint IS NOT OLD.operation_request_fingerprint OR
                    NEW.result_contract IS NOT OLD.result_contract OR
                    NEW.result_json IS NOT OLD.result_json OR
                    NEW.visible_result IS NOT OLD.visible_result OR
                    NEW.transport_json IS NOT OLD.transport_json OR
                    NEW.undo_json IS NOT OLD.undo_json OR
                    NEW.terminal_payload_sha256 IS NOT OLD.terminal_payload_sha256 OR
                    NEW.failure_category IS NOT OLD.failure_category OR
                    NEW.failure_code IS NOT OLD.failure_code OR
                    NEW.approved_at IS NOT OLD.approved_at OR
                    NEW.claimed_at IS NOT OLD.claimed_at OR
                    NEW.rejected_at IS NOT OLD.rejected_at OR
                    NEW.committed_at IS NOT OLD.committed_at OR
                    NEW.failed_at IS NOT OLD.failed_at
                )
                BEGIN
                    SELECT RAISE(ABORT, 'write operation terminal is immutable');
                END;
CREATE TRIGGER trg_write_operation_compensation_update
                BEFORE UPDATE OF parent_operation_id, operation_role ON write_operations
                WHEN NEW.operation_role = 'compensation'
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM write_operations parent
                        WHERE parent.id = NEW.parent_operation_id
                          AND parent.operation_role = 'primary'
                          AND parent.status = 'committed'
                          AND parent.terminal_payload_sha256 = NEW.parent_terminal_payload_sha256
                          AND length(NEW.parent_terminal_payload_sha256) = 71
                          AND substr(NEW.parent_terminal_payload_sha256,1,7) = 'sha256:'
                          AND substr(NEW.parent_terminal_payload_sha256,8) NOT GLOB '*[^0-9a-f]*'
                    ) THEN RAISE(ABORT, 'invalid compensation parent') END;
                END;
CREATE TRIGGER trg_write_operation_delivery_generation
                BEFORE UPDATE ON write_operations
                WHEN NEW.delivery_generation < OLD.delivery_generation
                  OR NEW.delivery_generation > OLD.delivery_generation + 1
                  OR (
                    NEW.delivery_generation = OLD.delivery_generation + 1
                    AND OLD.delivery_generation > 0
                    AND (
                      OLD.delivery_status <> 'pending'
                      OR OLD.delivery_lease_expires_at > unixepoch('now')
                      OR NEW.delivery_status <> 'pending'
                    )
                  )
                  OR (
                    OLD.status <> 'proposed'
                    AND NEW.delivery_generation = OLD.delivery_generation
                    AND NEW.delivery_status = 'pending'
                    AND NEW.delivery_owner_token_fingerprint IS NOT OLD.delivery_owner_token_fingerprint
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid delivery generation');
                END;
CREATE TRIGGER trg_write_operation_delivery_immutable
                BEFORE UPDATE ON write_operations
                WHEN OLD.delivery_status IN ('completed','failed','not_applicable') AND (
                    NEW.delivery_status IS NOT OLD.delivery_status OR
                    NEW.delivery_failure_code IS NOT OLD.delivery_failure_code OR
                    NEW.delivery_outcome IS NOT OLD.delivery_outcome OR
                    NEW.delivery_message_count IS NOT OLD.delivery_message_count OR
                    NEW.delivery_manifest_sha256 IS NOT OLD.delivery_manifest_sha256 OR
                    NEW.delivery_next_operation_id IS NOT OLD.delivery_next_operation_id OR
                    NEW.delivery_generation IS NOT OLD.delivery_generation OR
                    NEW.delivery_owner_token_fingerprint IS NOT OLD.delivery_owner_token_fingerprint OR
                    NEW.delivery_lease_expires_at IS NOT OLD.delivery_lease_expires_at OR
                    NEW.delivered_at IS NOT OLD.delivered_at
                )
                BEGIN
                    SELECT RAISE(ABORT, 'write operation delivery is immutable');
                END;
CREATE TRIGGER trg_write_operation_chained_delivery
                BEFORE UPDATE OF delivery_next_operation_id ON write_operations
                WHEN NEW.delivery_next_operation_id IS NOT NULL
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM write_operations child
                        WHERE child.id = NEW.delivery_next_operation_id
                          AND child.operation_role = 'primary'
                          AND child.status = 'proposed'
                          AND child.conversation_id = NEW.conversation_id
                    ) THEN RAISE(ABORT, 'invalid chained operation') END;
                END;
CREATE TRIGGER trg_write_operation_transition_insert
                BEFORE INSERT ON write_operation_transitions
                BEGIN
                    SELECT CASE WHEN NOT (
                      (NEW.seq = 1 AND NEW.state = 'proposed'
                       AND NOT EXISTS (SELECT 1 FROM write_operation_transitions
                                       WHERE operation_id = NEW.operation_id))
                      OR
                      (NEW.seq = 2 AND NEW.state IN ('approved','rejected')
                       AND EXISTS (SELECT 1 FROM write_operation_transitions
                                   WHERE operation_id = NEW.operation_id
                                     AND seq = 1 AND state = 'proposed')
                       AND EXISTS (SELECT 1 FROM write_operations
                                   WHERE id = NEW.operation_id
                                     AND ((NEW.state = 'approved' AND status = 'proposed')
                                          OR status = 'rejected')))
                      OR
                      (NEW.seq = 3 AND NEW.state = 'claimed'
                       AND EXISTS (SELECT 1 FROM write_operation_transitions
                                   WHERE operation_id = NEW.operation_id
                                     AND seq = 2 AND state = 'approved'))
                      OR
                      (NEW.seq = 4 AND NEW.state IN ('committed','failed')
                       AND EXISTS (SELECT 1 FROM write_operation_transitions
                                   WHERE operation_id = NEW.operation_id
                                     AND seq = 3 AND state = 'claimed')
                       AND EXISTS (SELECT 1 FROM write_operations
                                   WHERE id = NEW.operation_id AND status = NEW.state))
                    ) THEN RAISE(ABORT, 'invalid write operation transition') END;
                END;
CREATE TRIGGER trg_write_operation_transition_immutable
                BEFORE UPDATE ON write_operation_transitions
                BEGIN
                    SELECT RAISE(ABORT, 'write operation transition is immutable');
                END;
CREATE TRIGGER trg_write_operation_transition_delete
                BEFORE DELETE ON write_operation_transitions
                BEGIN
                    SELECT RAISE(ABORT, 'write operation transition is immutable');
                END;
CREATE TRIGGER trg_write_operation_chat_restrict
                BEFORE DELETE ON write_operations
                WHEN EXISTS (SELECT 1 FROM chat_messages
                             WHERE operation_id = OLD.id)
                BEGIN
                    SELECT RAISE(ABORT, 'operation delivery messages exist');
                END;
CREATE TRIGGER trg_chat_message_operation_insert
                BEFORE INSERT ON chat_messages
                BEGIN
                    SELECT CASE WHEN
                      (NEW.operation_id IS NULL AND
                       (NEW.delivery_kind IS NOT NULL OR NEW.delivery_ordinal IS NOT NULL))
                      OR
                      (NEW.operation_id IS NOT NULL AND (
                        NEW.delivery_kind IS NULL OR NEW.delivery_ordinal IS NULL OR
                        NOT (
                          (NEW.delivery_kind = 'origin_tool_result'
                           AND NEW.delivery_ordinal = 0
                           AND NEW.role = 'tool' AND NEW.tool_call_id <> '')
                          OR
                          (NEW.delivery_kind = 'continuation_message'
                           AND NEW.delivery_ordinal >= 1
                           AND ((NEW.role = 'tool' AND NEW.tool_call_id <> '')
                                OR (NEW.role = 'assistant' AND NEW.tool_call_id = '')))
                        ) OR NOT EXISTS (
                        SELECT 1 FROM write_operations op
                        WHERE op.id = NEW.operation_id
                          AND op.conversation_id = NEW.conversation_id
                          AND (NEW.delivery_kind <> 'origin_tool_result'
                               OR op.tool_call_id = NEW.tool_call_id)
                        )
                      ))
                    THEN RAISE(ABORT, 'invalid operation delivery message') END;
                END;
CREATE TRIGGER trg_chat_message_operation_update
                BEFORE UPDATE OF operation_id, conversation_id, role, content, tool_calls,
                    tool_call_id, provider_blocks, delivery_kind, delivery_ordinal
                ON chat_messages
                WHEN OLD.operation_id IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'operation delivery message is immutable');
                END;
CREATE TRIGGER trg_chat_message_operation_bind
                BEFORE UPDATE OF operation_id, conversation_id, role, content, tool_calls,
                    tool_call_id, provider_blocks, delivery_kind, delivery_ordinal
                ON chat_messages
                WHEN OLD.operation_id IS NULL
                BEGIN
                    SELECT CASE WHEN
                      (NEW.operation_id IS NULL AND
                       (NEW.delivery_kind IS NOT NULL OR NEW.delivery_ordinal IS NOT NULL))
                      OR
                      (NEW.operation_id IS NOT NULL AND (
                        NEW.delivery_kind IS NULL OR NEW.delivery_ordinal IS NULL OR
                        NOT (
                          (NEW.delivery_kind = 'origin_tool_result'
                           AND NEW.delivery_ordinal = 0
                           AND NEW.role = 'tool' AND NEW.tool_call_id <> '')
                          OR
                          (NEW.delivery_kind = 'continuation_message'
                           AND NEW.delivery_ordinal >= 1
                           AND ((NEW.role = 'tool' AND NEW.tool_call_id <> '')
                                OR (NEW.role = 'assistant' AND NEW.tool_call_id = '')))
                        ) OR NOT EXISTS (
                          SELECT 1 FROM write_operations op
                          WHERE op.id = NEW.operation_id
                            AND op.conversation_id = NEW.conversation_id
                            AND (NEW.delivery_kind <> 'origin_tool_result'
                                 OR op.tool_call_id = NEW.tool_call_id)
                        )
                      ))
                    THEN RAISE(ABORT, 'invalid operation delivery message') END;
                END;
CREATE INDEX idx_offer_negotiation_proposals_status ON offer_negotiation_proposals(attempt_status);
CREATE INDEX idx_notes_event ON interview_notes(application_event_id);
CREATE UNIQUE INDEX uq_interview_notes_event_main ON interview_notes(application_event_id) WHERE application_event_id IS NOT NULL;
CREATE UNIQUE INDEX uq_knowledge_active_job_source_kind
                ON knowledge_jobs (source_id, kind)
                WHERE source_id IS NOT NULL
                  AND kind IN ('extract', 'delete')
                  AND status IN ('pending', 'running')
                  AND canceled = 0
                ;
CREATE UNIQUE INDEX uq_knowledge_active_brief_source_snapshot
                ON knowledge_jobs (source_id, snapshot_id)
                WHERE source_id IS NOT NULL
                  AND kind = 'brief'
                  AND snapshot_id IS NOT NULL
                  AND status IN ('pending', 'running')
                  AND canceled = 0
                ;
CREATE UNIQUE INDEX uq_knowledge_active_attempt_source
                ON knowledge_brief_attempts (source_id)
                WHERE status IN ('pending', 'processing')
                ;
CREATE TRIGGER trg_knowledge_source_snapshot_ref
            BEFORE UPDATE OF active_snapshot_id ON knowledge_sources
            WHEN NEW.active_snapshot_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.active_snapshot_id AND source_id = NEW.id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_source_active_snapshot_mismatch');
            END;
CREATE TRIGGER trg_knowledge_source_brief_ref
            BEFORE UPDATE OF active_brief_id ON knowledge_sources
            WHEN NEW.active_brief_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_source_briefs
                WHERE id = NEW.active_brief_id AND source_id = NEW.id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_source_active_brief_mismatch');
            END;
CREATE TRIGGER trg_knowledge_evidence_snapshot_ref
            BEFORE INSERT ON knowledge_evidence
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_snapshot_mismatch');
            END;
CREATE TRIGGER trg_knowledge_evidence_asset_ref
            BEFORE INSERT ON knowledge_evidence
            WHEN NEW.asset_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_source_assets
                WHERE id = NEW.asset_id AND source_id = NEW.source_id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_asset_mismatch');
            END;
CREATE TRIGGER trg_knowledge_evidence_asset_ref_update
            BEFORE UPDATE OF asset_id, source_id ON knowledge_evidence
            WHEN NEW.asset_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_source_assets
                WHERE id = NEW.asset_id AND source_id = NEW.source_id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_asset_mismatch');
            END;
CREATE TRIGGER trg_knowledge_evidence_neighbor_ref
            BEFORE UPDATE OF previous_evidence_id, next_evidence_id ON knowledge_evidence
            WHEN (
                NEW.previous_evidence_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM knowledge_evidence
                    WHERE id = NEW.previous_evidence_id
                      AND source_id = NEW.source_id
                      AND snapshot_id = NEW.snapshot_id
                )
            ) OR (
                NEW.next_evidence_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM knowledge_evidence
                    WHERE id = NEW.next_evidence_id
                      AND source_id = NEW.source_id
                      AND snapshot_id = NEW.snapshot_id
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_neighbor_mismatch');
            END;
CREATE TRIGGER trg_knowledge_evidence_neighbor_ref_insert
            BEFORE INSERT ON knowledge_evidence
            WHEN (
                NEW.previous_evidence_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM knowledge_evidence
                    WHERE id = NEW.previous_evidence_id
                      AND source_id = NEW.source_id
                      AND snapshot_id = NEW.snapshot_id
                )
            ) OR (
                NEW.next_evidence_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM knowledge_evidence
                    WHERE id = NEW.next_evidence_id
                      AND source_id = NEW.source_id
                      AND snapshot_id = NEW.snapshot_id
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_neighbor_mismatch');
            END;
CREATE TRIGGER trg_knowledge_job_snapshot_ref
            BEFORE INSERT ON knowledge_jobs
            WHEN NEW.snapshot_id IS NOT NULL
             AND (
                NEW.source_id IS NULL
                OR NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
                )
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_job_snapshot_mismatch');
            END;
CREATE TRIGGER trg_knowledge_job_snapshot_ref_update
            BEFORE UPDATE OF snapshot_id, source_id ON knowledge_jobs
            WHEN NEW.snapshot_id IS NOT NULL
             AND (
                NEW.source_id IS NULL
                OR NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
                )
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_job_snapshot_mismatch');
            END;
CREATE TRIGGER trg_knowledge_job_attempt_ref
            BEFORE INSERT ON knowledge_jobs
            WHEN NEW.attempt_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_brief_attempts
                WHERE id = NEW.attempt_id
                  AND source_id = NEW.source_id
                  AND snapshot_id = NEW.snapshot_id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_job_attempt_mismatch');
            END;
CREATE TRIGGER trg_knowledge_job_attempt_ref_update
            BEFORE UPDATE OF attempt_id, source_id, snapshot_id ON knowledge_jobs
            WHEN NEW.attempt_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_brief_attempts
                WHERE id = NEW.attempt_id
                  AND source_id = NEW.source_id
                  AND snapshot_id = NEW.snapshot_id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_job_attempt_mismatch');
            END;
CREATE TRIGGER trg_knowledge_brief_snapshot_ref
            BEFORE INSERT ON knowledge_source_briefs
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_snapshot_mismatch');
            END;
CREATE TRIGGER trg_knowledge_brief_snapshot_ref_update
            BEFORE UPDATE OF snapshot_id, source_id ON knowledge_source_briefs
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_snapshot_mismatch');
            END;
CREATE TRIGGER trg_knowledge_brief_attempt_snapshot_ref
            BEFORE INSERT ON knowledge_brief_attempts
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_attempt_snapshot_mismatch');
            END;
CREATE TRIGGER trg_knowledge_brief_attempt_snapshot_ref_update
            BEFORE UPDATE OF snapshot_id, source_id ON knowledge_brief_attempts
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_attempt_snapshot_mismatch');
            END;
CREATE TRIGGER trg_knowledge_brief_attempt_ref
            BEFORE INSERT ON knowledge_source_briefs
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_brief_attempts
                WHERE id = NEW.winning_attempt_id
                  AND source_id = NEW.source_id
                  AND snapshot_id = NEW.snapshot_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_attempt_mismatch');
            END;
CREATE TRIGGER trg_knowledge_brief_attempt_ref_update
            BEFORE UPDATE OF winning_attempt_id, source_id, snapshot_id
                ON knowledge_source_briefs
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_brief_attempts
                WHERE id = NEW.winning_attempt_id
                  AND source_id = NEW.source_id
                  AND snapshot_id = NEW.snapshot_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_attempt_mismatch');
            END;
CREATE TRIGGER trg_conversations_scope_insert
                BEFORE INSERT ON conversations
                BEGIN
                    SELECT CASE WHEN
                        typeof(NEW.scope_revision) <> 'integer'
                        OR NEW.scope_revision <> 0
                    THEN RAISE(ABORT, 'conversation scope revision must start at zero') END;
                    SELECT CASE WHEN
                        typeof(NEW.scope_revision) <> 'integer'
                        OR NEW.scope_revision < 0
                        OR NEW.scope_revision > 9223372036854775807
                    THEN RAISE(ABORT, 'conversation scope revision is out of range') END;
                END;
CREATE TRIGGER trg_conversations_scope_update
                BEFORE UPDATE ON conversations
                WHEN NEW.context_type IS NOT OLD.context_type
                  OR NEW.context_ref IS NOT OLD.context_ref
                  OR NEW.mode IS NOT OLD.mode
                BEGIN
                    SELECT CASE WHEN OLD.scope_revision = 9223372036854775807
                        THEN RAISE(ABORT, 'conversation scope revision overflow') END;
                    SELECT CASE WHEN
                        typeof(NEW.scope_revision) <> 'integer'
                        OR NEW.scope_revision < 0
                        OR NEW.scope_revision > 9223372036854775807
                        OR NEW.scope_revision <> OLD.scope_revision + 1
                    THEN RAISE(ABORT, 'conversation scope revision must increment') END;
                END;
CREATE TRIGGER trg_conversations_scope_revision_unchanged
                BEFORE UPDATE ON conversations
                WHEN NEW.context_type IS OLD.context_type
                  AND NEW.context_ref IS OLD.context_ref
                  AND NEW.mode IS OLD.mode
                  AND NEW.scope_revision IS NOT OLD.scope_revision
                BEGIN
                    SELECT RAISE(ABORT, 'conversation scope revision changed without scope mutation');
                END;
CREATE TRIGGER trg_conversations_mode_insert
                BEFORE INSERT ON conversations
                BEGIN
                    SELECT CASE WHEN NOT (
        typeof(NEW.mode) = 'text'
        AND EXISTS (
            WITH RECURSIVE
              mode_input(hex_bytes) AS (
                SELECT hex(CAST(NEW.mode AS BLOB))
              ),
              mode_scan(byte_pos, code_points, is_valid) AS (
                SELECT 1, 0, 1
                UNION ALL
                SELECT
                  byte_pos + CASE
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN '20' AND '7E' THEN 2
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'C2' AND 'DF' THEN 4
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'E0' AND 'EF' THEN 6
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'F0' AND 'F4' THEN 8
                    ELSE 2
                  END,
                  code_points + 1,
                  CASE
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN '20' AND '7E'
                      THEN 1
                    WHEN substr(hex_bytes, byte_pos, 2) = 'C2'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN 'A0' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'C3' AND 'DF'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'E0'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN 'A0' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'E1' AND 'EC'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'ED'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND '9F'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'EE' AND 'EF'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'F0'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '90' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 6, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'F1' AND 'F3'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 6, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'F4'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND '8F'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 6, 2) BETWEEN '80' AND 'BF'
                    ELSE 0
                  END
                FROM mode_scan, mode_input
                WHERE is_valid
                  AND byte_pos <= length(hex_bytes)
                  AND code_points < 64
                  AND length(hex_bytes) <= 512
              )
            SELECT 1
            FROM mode_scan, mode_input
            WHERE is_valid
              AND byte_pos = length(hex_bytes) + 1
              AND code_points BETWEEN 1 AND 64
              AND length(hex_bytes) <= 512
        )
        AND NEW.mode = trim(NEW.mode)
        AND unicode(substr(NEW.mode, 1, 1)) NOT IN (160, 5760, 8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202, 8232, 8233, 8239, 8287, 12288)
        AND unicode(substr(NEW.mode, -1, 1)) NOT IN (160, 5760, 8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202, 8232, 8233, 8239, 8287, 12288)
    )
                      THEN RAISE(ABORT, 'invalid conversation mode') END;
                END;
CREATE TRIGGER trg_conversations_mode_update
                BEFORE UPDATE OF mode ON conversations
                BEGIN
                    SELECT CASE WHEN NOT (
        typeof(NEW.mode) = 'text'
        AND EXISTS (
            WITH RECURSIVE
              mode_input(hex_bytes) AS (
                SELECT hex(CAST(NEW.mode AS BLOB))
              ),
              mode_scan(byte_pos, code_points, is_valid) AS (
                SELECT 1, 0, 1
                UNION ALL
                SELECT
                  byte_pos + CASE
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN '20' AND '7E' THEN 2
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'C2' AND 'DF' THEN 4
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'E0' AND 'EF' THEN 6
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'F0' AND 'F4' THEN 8
                    ELSE 2
                  END,
                  code_points + 1,
                  CASE
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN '20' AND '7E'
                      THEN 1
                    WHEN substr(hex_bytes, byte_pos, 2) = 'C2'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN 'A0' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'C3' AND 'DF'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'E0'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN 'A0' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'E1' AND 'EC'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'ED'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND '9F'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'EE' AND 'EF'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'F0'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '90' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 6, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'F1' AND 'F3'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 6, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'F4'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND '8F'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 6, 2) BETWEEN '80' AND 'BF'
                    ELSE 0
                  END
                FROM mode_scan, mode_input
                WHERE is_valid
                  AND byte_pos <= length(hex_bytes)
                  AND code_points < 64
                  AND length(hex_bytes) <= 512
              )
            SELECT 1
            FROM mode_scan, mode_input
            WHERE is_valid
              AND byte_pos = length(hex_bytes) + 1
              AND code_points BETWEEN 1 AND 64
              AND length(hex_bytes) <= 512
        )
        AND NEW.mode = trim(NEW.mode)
        AND unicode(substr(NEW.mode, 1, 1)) NOT IN (160, 5760, 8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202, 8232, 8233, 8239, 8287, 12288)
        AND unicode(substr(NEW.mode, -1, 1)) NOT IN (160, 5760, 8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202, 8232, 8233, 8239, 8287, 12288)
    )
                      THEN RAISE(ABORT, 'invalid conversation mode') END;
                END;
CREATE TRIGGER trg_write_operation_scope_insert
                BEFORE INSERT ON write_operations
                WHEN (
                    NEW.operation_role = 'primary'
                    AND NEW.adapter_kind = 'typed'
                    AND NEW.status = 'proposed'
                    AND NEW.authorization_scope_fingerprint IS NULL
                  ) OR (
                    NEW.authorization_scope_fingerprint IS NOT NULL
                    AND (
                      length(NEW.authorization_scope_fingerprint) <> 76
                      OR substr(NEW.authorization_scope_fingerprint,1,12) <> 'hmac-sha256:'
                      OR substr(NEW.authorization_scope_fingerprint,13) GLOB '*[^0-9a-f]*'
                    )
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid authorization scope fingerprint');
                END;
CREATE TRIGGER trg_write_operation_scope_fingerprint_immutable
                BEFORE UPDATE OF authorization_scope_fingerprint ON write_operations
                WHEN NEW.authorization_scope_fingerprint IS NOT OLD.authorization_scope_fingerprint
                BEGIN
                    SELECT RAISE(ABORT, 'authorization scope fingerprint is immutable');
                END;
CREATE TRIGGER trg_write_operation_scope_identity_immutable
                BEFORE UPDATE ON write_operations
                WHEN NEW.operation_role IS NOT OLD.operation_role
                  OR NEW.adapter_kind IS NOT OLD.adapter_kind
                  OR NEW.tool_name IS NOT OLD.tool_name
                  OR NEW.tool_call_id IS NOT OLD.tool_call_id
                  OR NEW.fingerprint_key_id IS NOT OLD.fingerprint_key_id
                  OR NEW.proposal_fingerprint IS NOT OLD.proposal_fingerprint
                  OR NEW.confirmation_token_fingerprint IS NOT OLD.confirmation_token_fingerprint
                  OR NEW.parent_operation_id IS NOT OLD.parent_operation_id
                BEGIN
                    SELECT RAISE(ABORT, 'write operation authority identity is immutable');
                END;
CREATE TRIGGER trg_write_operation_scope_conversation_immutable
                BEFORE UPDATE OF conversation_id ON write_operations
                WHEN (OLD.conversation_id IS NULL AND NEW.conversation_id IS NOT NULL)
                  OR (OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS NOT NULL
                      AND NEW.conversation_id <> OLD.conversation_id)
                BEGIN
                    SELECT RAISE(ABORT, 'write operation conversation binding is immutable');
                END;
CREATE TRIGGER trg_write_operation_scope_status
                BEFORE UPDATE OF status ON write_operations
                WHEN OLD.operation_role = 'primary'
                  AND OLD.adapter_kind = 'typed'
                  AND OLD.status = 'proposed'
                  AND OLD.authorization_scope_fingerprint IS NULL
                  AND NEW.status NOT IN ('proposed','rejected')
                BEGIN
                    SELECT RAISE(ABORT, 'unbound typed operation cannot become terminal');
                END;
PRAGMA writable_schema=OFF;
DELETE FROM "sqlite_sequence";
COMMIT;
