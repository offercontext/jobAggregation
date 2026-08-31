export type InterviewStoryStatus = 'active' | 'archived';
export type InterviewStoryAttemptStatus = 'generating' | 'provider_unknown' | 'ready' | 'safe_empty' | 'contract_failed' | 'invalidated' | 'confirmed';
export type InterviewStoryTargetKind = 'title' | 'block' | 'capability_label' | 'applicable_question';
export type InterviewStorySourceKind = 'resume_version' | 'interview_note' | 'mock_turn' | 'user_assertion';

export interface InterviewStoryBlock {
  id: string;
  kind: 'situation' | 'task' | 'action' | 'result' | 'reflection';
  text: string;
  fact_mode: 'evidence_backed' | 'user_view';
}

export interface InterviewStoryContent {
  title: { id: 'title'; text: string };
  blocks: InterviewStoryBlock[];
  capability_labels: Array<{ id: string; text: string }>;
  applicable_questions: Array<{ id: string; text: string }>;
  fact_gap_codes: string[];
}

export interface InterviewStoryEditableContent {
  title: string;
  blocks: Array<Pick<InterviewStoryBlock, 'kind' | 'text' | 'fact_mode'>>;
  capability_labels: string[];
  applicable_questions: string[];
  fact_gap_codes: string[];
}

export interface InterviewStoryEvidenceLink {
  target_kind: InterviewStoryTargetKind;
  target_id: string;
  source_kind: InterviewStorySourceKind;
  source_stable_id: string;
  source_version_or_snapshot: string;
  source_path: string;
  excerpt: string;
  text_location?: string;
}

export interface InterviewStorySourceState {
  source_kind: InterviewStorySourceKind;
  source_stable_id: string;
  source_version_or_snapshot: string;
  state: 'current' | 'changed' | 'missing' | 'frozen_user_assertion';
}

export interface InterviewStoryVersion {
  id: number;
  version_number: number;
  origin_kind: 'manual' | 'proposal';
  confirmed_at: string | null;
  source_fingerprint: string;
  content: InterviewStoryContent;
  evidence_links: InterviewStoryEvidenceLink[];
  assertions: Array<{ id: number; statement: string; frozen: true }>;
  source_states: InterviewStorySourceState[];
}

export interface InterviewStory {
  id: number;
  title: string;
  status: InterviewStoryStatus;
  current_version_id: number | null;
  story_revision: number;
  version_number: number | null;
  source_states: InterviewStorySourceState[];
  version?: InterviewStoryVersion | null;
}

export interface InterviewStorySourceSelection {
  source_kind: Exclude<InterviewStorySourceKind, 'user_assertion'>;
  source_id: number;
  path: string;
}

export interface InterviewStorySourceCandidateLeaf {
  path: string;
  preview: string;
}

export interface InterviewStoryResumeSourceCandidate {
  id: number;
  label: string;
  leaves: InterviewStorySourceCandidateLeaf[];
}

export interface InterviewStoryNoteSourceCandidate extends InterviewStoryResumeSourceCandidate {}

export interface InterviewStoryMockTurnSourceCandidate {
  attempt_id: number;
  turn_no: number;
  label: string;
  leaves: InterviewStorySourceCandidateLeaf[];
}

export interface InterviewStorySourceCandidates {
  resumes: InterviewStoryResumeSourceCandidate[];
  interview_notes: InterviewStoryNoteSourceCandidate[];
  mock_turns: InterviewStoryMockTurnSourceCandidate[];
}

export interface InterviewStoryClientEvidenceLink {
  target_kind: InterviewStoryTargetKind;
  target_id: string;
  source_kind: InterviewStorySourceKind;
  source_id: number | string;
  source_path: string;
  excerpt: string;
  text_location?: string;
}

export interface InterviewStoryProposalInput {
  target_story_id: number | null;
  expected_current_version_id: number | null;
  expected_story_revision: number | null;
  selections: InterviewStorySourceSelection[];
  assertions: string[];
  idempotency_key: string;
  entry_context?: { review_note_id?: number };
}

export interface InterviewStoryManualInput {
  content: InterviewStoryEditableContent;
  evidence_links: InterviewStoryClientEvidenceLink[];
  selections: InterviewStorySourceSelection[];
  assertions: string[];
  expected_current_version_id: number | null;
  idempotency_key: string;
}

export interface InterviewStoryPendingAttempt {
  id: number;
  attempt_status: 'generating' | 'provider_unknown';
  generation_revision: number;
  source_fingerprint: string;
  retry_after_ms: number;
}

export interface InterviewStoryProductActionControl {
  operation_id: string;
  action_call_id: string;
  confirmation_token: string;
  action_name: 'confirm_interview_story';
  allowed_decisions?: Array<'approve' | 'modify' | 'reject'>;
  rejection_only?: boolean;
}

export interface InterviewStoryProductActionTerminal {
  operation_id: string;
  action_name: 'confirm_interview_story';
  status: 'rejected' | 'committed' | 'failed';
  terminal_result?: Record<string, unknown>;
}

export type InterviewStoryProductAction = InterviewStoryProductActionControl | InterviewStoryProductActionTerminal;

interface InterviewStoryNextProductActionBase {
  schema_version: 1;
  contract: 'story_product_action_proposal_response_v1';
  operation_id: string;
  action_call_id: string;
  product_action_generation: number;
}

export type InterviewStoryNextProductActionResponse = InterviewStoryNextProductActionBase & (
  | {
      status: 'proposed';
      proposal_created: boolean;
      confirmation_token: string;
      terminal_result?: never;
    }
  | {
      status: 'rejected' | 'committed' | 'failed';
      proposal_created: false;
      confirmation_token?: never;
      terminal_result: Record<string, unknown>;
    }
);

export interface InterviewStoryProposalAttempt {
  id: number;
  attempt_status: InterviewStoryAttemptStatus;
  generation_revision: number;
  source_fingerprint: string;
  proposal?:
    | { proposal_status: 'normal' | 'safe_empty'; content: InterviewStoryContent; evidence_links: InterviewStoryEvidenceLink[] }
    | { proposal_status: 'manual' }
    | null;
  proposal_hash?: string | null;
  product_action_generation?: number;
  product_action?: InterviewStoryProductAction;
  target_story_id?: number | null;
  entrypoint?: 'ui' | 'pilot';
  failure_category?: string | null;
}

export class InterviewStoryError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string | null,
    /** Safe server identity required to resume a provider-unknown Attempt. */
    public readonly attemptId: number | null = null,
    /** Safe delay before the same fenced Attempt may be replayed. */
    public readonly retryAfterMs: number | null = null,
  ) {
    super(code ?? 'interview_story_error');
    this.name = 'InterviewStoryError';
  }
}
