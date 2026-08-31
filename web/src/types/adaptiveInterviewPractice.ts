export type AdaptivePracticeDrillKind =
  | 'difficulty_breakdown'
  | 'answer_reframe'
  | 'question_decode'
  | 'pressure_rehearsal';

export type AdaptivePracticeSourceStatus = 'current' | 'changed' | 'missing';
export type AdaptivePracticeAssessment = 'needs_work' | 'clearer' | 'confident';

export interface AdaptivePracticeRecommendation {
  proposal_id: number;
  focus_id: string;
  application_id: number;
  application_event_id: number;
  interview_note_id: number;
  company_name: string;
  position_name: string;
  drill_kind: AdaptivePracticeDrillKind;
  title: string;
  observation: string;
  reason: string;
  prompt: string;
  source_path: string;
  source_excerpt: string;
  source_fingerprint: string;
}

export interface AdaptivePracticePlan extends AdaptivePracticeRecommendation {
  id: number;
  origin_contract: 'legacy_review_focus_v1' | 'confirmed_readiness_signal_v1';
  target_application_event_id: number | null;
  readiness_signal_version_id: number | null;
  target_fingerprint: string | null;
  practice_state: 'ready' | 'in_progress' | 'completed' | 'source_changed' | 'source_missing' | 'target_changed' | 'target_missing' | 'retracted' | 'not_eligible' | 'unavailable';
  status: 'in_progress' | 'completed';
  revision: number;
  source_status: AdaptivePracticeSourceStatus;
  response_text: string;
  reflection_text: string;
  self_assessment: AdaptivePracticeAssessment | '';
  created_at: string;
  completed_at: string | null;
}

export interface AdaptivePracticeFocus {
  ownerGeneration: number;
  signalVersionId: number;
  targetEventId: number;
}

export interface AdaptivePracticeV2StartInput {
  readiness_signal_version_id: number;
  target_application_event_id: number;
  expected_source_fingerprint: string;
  expected_target_fingerprint: string;
  idempotency_key: string;
}

export interface AdaptivePracticeCompleteInput {
  expected_revision: number;
  response_text: string;
  reflection_text: string;
  self_assessment: AdaptivePracticeAssessment;
  idempotency_key: string;
}

export interface AdaptivePracticeOwnerDraft {
  ownerKey: string;
  ownerGeneration: number;
  signalVersionId: number | null;
  targetEventId: number | null;
  planId: number | null;
  answer: string;
  reflection: string;
  assessment: AdaptivePracticeAssessment | null;
  startInput: AdaptivePracticeV2StartInput | null;
  completionInput: AdaptivePracticeCompleteInput | null;
  resultUnknown: boolean;
  pendingOperation: 'start' | 'complete' | null;
}
