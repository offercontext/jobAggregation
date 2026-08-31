export type InterviewPreparationEvidenceSource = 'jd' | 'resume' | 'knowledge_evidence';

export interface InterviewPreparationEvidenceRef {
  source: InterviewPreparationEvidenceSource;
  path: string;
  excerpt: string;
}

export interface InterviewPreparationItem {
  id: string;
  text: string;
  evidence_refs: InterviewPreparationEvidenceRef[];
}

export interface InterviewPreparationProposalBody {
  preparation_directions: InterviewPreparationItem[];
  story_prompts: InterviewPreparationItem[];
  review_points: InterviewPreparationItem[];
  interviewer_questions: InterviewPreparationItem[];
  items_to_clarify: InterviewPreparationItem[];
}

export interface InterviewPreparationProposal {
  id: number;
  application_id: number;
  event_id: number;
  resume_id: number;
  attempt_status: 'ready';
  proposal_status: 'normal' | 'safe_empty';
  source_fingerprint: string;
  source_status: 'current' | 'source_changed' | 'not_checked';
  source_states: Record<string, string>;
  proposal: InterviewPreparationProposalBody;
  proposal_hash: string;
  created_at: string;
}

export interface InterviewPreparationPendingResponse {
  attempt_status: 'generating' | 'provider_unknown';
  application_id: number;
  event_id: number;
  idempotency_key: string;
  generation_revision: number;
  retry_after_ms: number;
}

export type InterviewPreparationResponse =
  | InterviewPreparationProposal
  | InterviewPreparationPendingResponse;

export interface CreateInterviewPreparationProposalInput {
  application_id: number;
  event_id: number;
  resume_id: number;
  jd_version_id: number;
  knowledge_selections: Array<Record<string, unknown>>;
  user_assertions: string[];
  idempotency_key: string;
  readiness_feedback_version_ids?: number[];
}

export class InterviewPreparationProposalError extends Error {
  status: number;
  code: string | null;

  constructor(status: number, code: string | null) {
    super(code || 'interview_preparation_unknown_error');
    this.name = 'InterviewPreparationProposalError';
    this.status = status;
    this.code = code;
  }
}

export function isInterviewPreparationPending(
  response: InterviewPreparationResponse,
): response is InterviewPreparationPendingResponse {
  return response.attempt_status === 'generating' || response.attempt_status === 'provider_unknown';
}
