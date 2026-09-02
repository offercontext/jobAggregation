import type { ViewMode } from '@/layout/navigation';

export interface PilotPageContext {
  view: ViewMode;
  label: string;
  entity?: {
    kind: 'application' | 'offer';
    id: string;
    label: string;
    description?: string;
  };
  filters?: Array<{
    key: string;
    label: string;
    value: string;
  }>;
}

export interface PilotContextChip {
  key: string;
  label: string;
  value: string;
}

export type PilotAttachmentKind = 'application' | 'offer' | 'resume';

export interface PilotContextAttachment {
  kind: PilotAttachmentKind;
  id: string;
  label: string;
}

export type PilotActionRequest =
  | { type: 'application_jd_save'; jdText?: string; sourceUrl?: string | null }
  | {
      type: 'application_submission_snapshot'; resumeId: number; jdVersionId: number;
      materialKitId: number | null; submittedAt: string; note: string;
    }
  | {
      type: 'application_outcome_record'; snapshotId: number; eventId: number | null;
      stage: import('./applicationOutcome').ApplicationOutcomeStage;
      result: import('./applicationOutcome').ApplicationOutcomeResult;
      feedbackText: string; reflectionText: string; nextActionText: string;
      feedbackTags: import('./applicationOutcome').ApplicationFeedbackTag[]; occurredAt: string;
    };

export interface Conversation {
  id: number;
  title: string;
  title_source?: 'fallback' | 'generated' | 'manual' | string;
  mode?: string;
  context_type: string;
  context_ref: string;
  context_label?: string;
  pinned_at?: string | null;
  archived_at?: string | null;
  pending_action?: PendingAction | null;
  pending_clarification?: PendingAction | null;
  last_write_undo?: ChatUndo | null;
  created_at: string;
  updated_at: string;
}

export interface ChatStartRequest {
  requestKey: number;
  context_type: 'application';
  context_ref: string;
  context_label: string;
  mode: 'general' | 'nego_coach';
  /** Trusted request-scoped references that must accompany the first and follow-up sends. */
  attachments?: PilotContextAttachment[];
  /** A user-editable, unsent composer value. Never starts a request by itself. */
  composerDraft?: string;
  /** An explicit deterministic action that retains the existing auto-send contract. */
  initialMessage?: string;
  pilot_action?: PilotActionRequest;
}

export type WriteStatus = 'success' | 'failed' | 'cancelled' | 'none';

export interface ChatMessage {
  id: number;
  conversation_id: number;
  role: 'user' | 'assistant' | 'tool';
  content: string;
  tool_calls?: string;
  tool_call_id?: string;
  created_at: string;
}

export interface PendingAction {
  operation_id?: string;
  tool_name: string;
  human: string;
  confirmation_token: string;
  args?: Record<string, unknown>;
  editable_fields?: PendingActionEditableField[];
  target?: PendingActionTarget;
  proposed_changes?: PendingActionChange[];
  evidence?: PendingActionEvidence[];
  risk_hint?: string;
  workflow?: PendingActionWorkflow;
  draft_summary?: PendingActionDraftSummary;
  application_jd?: {
    current_version_number: number | null;
    proposed_version_number: number;
  };
}

export type EditableFieldType = 'string' | 'long_text' | 'number' | 'boolean' | 'enum' | 'datetime';

export interface PendingActionEditableField {
  field: string;
  type: EditableFieldType;
  options?: string[];
  clearable?: boolean;
  clear_value?: string | number | boolean | null;
}

export interface PendingActionDraftSummary {
  title: string;
  fields: Array<{
    field: string;
    label: string;
    summary: string;
    characters: number;
  }>;
}

export interface PendingActionWorkflow {
  current_step: number;
  total_steps: number;
  current_label: string;
  next_label?: string;
  description?: string;
}

export interface PendingActionTarget {
  id: string;
  kind: string;
  title: string;
  meta?: string;
  snippet?: string;
  source: string;
}

export interface PendingActionChange {
  field: string;
  before?: string | number | boolean | null;
  after?: string | number | boolean | null;
}

export interface PendingActionEvidence extends PendingActionTarget {}

export interface ChatUndo {
  kind: string;
  label: string;
  parent_operation_id?: string;
  [key: string]: unknown;
}

export type ChatResponse =
  | {
      type: 'message';
      conversation_id: number;
      message: string;
      degraded?: boolean;
      undo?: ChatUndo | null;
      write_status?: WriteStatus;
      write_error?: string;
      operation_id?: string;
      replayed?: boolean;
    }
  | { type: 'confirmation_required'; conversation_id: number; pending_action: PendingAction };

export type ChatStreamEventName =
  | 'meta'
  | 'user_message_saved'
  | 'status'
  | 'tool_call'
  | 'tool_result'
  | 'confirmation_required'
  | 'assistant_delta'
  | 'assistant_message'
  | 'completed'
  | 'error'
  | 'cancelled';

export interface ChatStreamEvent<TData = Record<string, unknown>> {
  run_id?: string;
  seq: number;
  conversation_id?: number;
  event: ChatStreamEventName | string;
  ts?: string;
  context_type?: string;
  context_ref?: string;
  mode?: string;
  data: TData;
}
