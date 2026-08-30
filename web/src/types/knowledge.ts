export interface KnowledgeEvidence {
  id: string;
  source_id: number;
  snapshot_id: number;
  kind: string;
  block_kind: string;
  ordinal: number;
  heading_path: string[];
  char_start: number;
  char_end: number;
  line_start: number;
  line_end: number;
  canonical_excerpt: string;
  search_text: string;
  content_hash: string;
  asset_id: number | null;
  previous_evidence_id: string | null;
  next_evidence_id: string | null;
  source_provenance?: KnowledgeSourceProvenance;
}

export interface KnowledgeEvidencePage {
  items: KnowledgeEvidence[];
  next_cursor: number | null;
}

export interface KnowledgeSourceAsset {
  id: number;
  source_id: number;
  logical_name: string;
  media_type: string;
  relative_path: string;
  bytes: number;
  sha256: string;
  width: number;
  height: number;
  created_at: string;
}

export interface KnowledgeSourceAssetsResponse {
  items: KnowledgeSourceAsset[];
}

export interface KnowledgeSourceProvenance {
  title?: string;
  author?: string;
  url?: string;
  published_at?: string;
  captured_at: string;
  metadata_extraction_version: string;
}

export interface KnowledgeEvidencePolicyRule {
  rule_id: string;
  label: string;
  count: number;
}

export interface KnowledgeEvidencePolicySummary {
  filtered_block_total: number;
  evidence_policy_version: string;
  rules: KnowledgeEvidencePolicyRule[];
}

export interface KnowledgeSource {
  id: number;
  source_kind: string;
  /** Present only for source rows that carry a typed interview-capture relation. */
  origin_kind?: string | null;
  capture_metadata?: KnowledgeCapturedSourceMetadata | null;
  captureMetadata?: KnowledgeCapturedSourceMetadata | null;
  title: string;
  display_title: string;
  title_hint: string;
  author: string;
  published_at: string | null;
  main_filename: string;
  main_media_type: string;
  total_bytes: number;
  token_count: number;
  lifecycle: string;
  extraction_status: string;
  extraction_error_code: string;
  extraction_error_message: string;
  brief_status: string;
  brief_block_reason: string;
  brief_error_code: string;
  brief_error_message: string;
  active_snapshot_id: number | null;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  provenance: KnowledgeSourceProvenance;
  evidence_policy_summary?: KnowledgeEvidencePolicySummary;
}

/**
 * Read-only relation metadata for a source captured from an interview note.
 * The metadata is optional on older source responses; consumers must fail
 * closed when a capture marker is present without a complete relation.
 */
export interface KnowledgeCapturedSourceMetadata {
  source_id?: number;
  origin_note_id: number;
  application_event_id: number | null;
  note_fingerprint: string;
  capture_schema_version: string;
  captured_at?: string;
}

export interface ConfirmedInterviewKnowledgeBlock {
  block_id: string;
  text: string;
  evidence_refs: Array<{ fragment_id: string; excerpt: string }>;
  evidence?: Array<{
    id: string;
    path: string;
    excerpt: string;
    char_start: number;
    char_end: number;
    line_start: number;
    line_end: number;
    frozen_at: string;
  }>;
}

export interface ConfirmedInterviewKnowledgeNote {
  id: number;
  title: string;
  origin_kind: 'confirmed_interview_capture';
  version_id: number;
  version_number: number;
  content: {
    title: string;
    blocks: ConfirmedInterviewKnowledgeBlock[];
  };
  source_id: number;
  capture_metadata?: KnowledgeCapturedSourceMetadata | null;
  captureMetadata?: KnowledgeCapturedSourceMetadata | null;
  source_status: 'frozen' | 'source_changed';
  captured_at: string;
  evidence?: Array<{
    id: string;
    path: string;
    excerpt: string;
    char_start: number;
    char_end: number;
    line_start: number;
    line_end: number;
    frozen_at: string;
  }>;
}

export interface KnowledgeJob {
  id: number;
  kind: string;
  queue: string;
  source_id: number | null;
  snapshot_id: number | null;
  stage: string;
  status: string;
  progress: number;
  retry_count: number;
  next_retry_at: string | null;
  error_code: string;
  error_message: string;
  canceled: boolean;
  lease_owner: string;
  lease_expires_at: string | null;
  heartbeat_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeOrigin {
  id: number;
  source_id: number;
  import_method: string;
  original_filename: string;
  origin_url: string;
  imported_at: string;
}

export interface KnowledgeIngestResponse {
  deduplicated: boolean;
  source: KnowledgeSource;
  job: KnowledgeJob;
  extraction_error_code: string;
  extraction_error_message: string;
}

export interface KnowledgeEvidenceSearchHit {
  evidence_id: string;
  source_id: number;
  snapshot_id: number;
  block_kind: string;
  heading_path: string[];
  char_start: number;
  char_end: number;
  line_start: number;
  line_end: number;
  canonical_excerpt: string;
  snippet: string;
  score: number;
  previous_evidence_id: string | null;
  next_evidence_id: string | null;
  source_provenance?: KnowledgeSourceProvenance;
}

export interface KnowledgeEvidenceSearchResponse {
  query: string;
  hits: KnowledgeEvidenceSearchHit[];
}

export interface KnowledgeSourceJobsResponse {
  jobs: KnowledgeJob[];
  origins: KnowledgeOrigin[];
}

export interface KnowledgeDeleteJob {
  id: number;
  kind: 'delete';
  queue: 'extraction';
  source_id: number;
  snapshot_id: number | null;
  stage: string;
  status: string;
  progress: number;
  retry_count: number;
  next_retry_at: string | null;
  error_code: string;
  error_message: string;
  canceled: boolean;
  lease_owner: string;
  lease_expires_at: string | null;
  heartbeat_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDeleteResponse {
  source_id: number;
  job: KnowledgeDeleteJob;
}

export interface BriefStatement {
  statement: string;
  evidence_ids: string[];
}

export interface BriefSectionGuide {
  section_key: string;
  heading_path: string[];
  summary: string;
  evidence_ids: string[];
}

export interface BriefCoverage {
  section_key: string;
  status: 'covered' | 'skipped';
  skipped_reason: string;
}

export interface BriefPayload {
  schema_version: number;
  language: string;
  overview: BriefStatement[];
  key_points: BriefStatement[];
  section_guides: BriefSectionGuide[];
  limitations: BriefStatement[];
  // KBR-04：coverage 由程序从实际 citations 派生并由 API 注入到 current Brief；
  // 候选 Brief（candidate_payload）未通过门禁，不带 coverage。
  coverage?: BriefCoverage[];
}

// KBR-05：结构化 validation report。失败详情按 issue_type 区分 citation/support/coverage，
// 每项可定位到候选 Brief block_path 与已引用 evidence_ids；不复制 Evidence 正文。
// Finding 4：reason 为程序生成的限长安全摘要，reason_code 为稳定原因码；模型原始 reason 不落库。
export interface BriefValidationIssue {
  block_path: string;
  issue_type: string;
  decision: string;
  reason_code?: string;
  // 旧版 API 的通用摘要；结构化诊断存在时优先展示 explanation。
  reason?: string;
  // KBR-08：结构化支持校验诊断，均为可选以兼容历史 Attempt。
  unsupported_fragments?: string[];
  explanation?: string;
  suggested_rewrite?: string;
  evidence_ids: string[];
}

export interface BriefValidationCoverageStatus {
  section_key: string;
  status: string;
  skipped_reason: string;
}

export interface BriefValidationReport {
  stage?: string;
  error_code?: string;
  failure_count?: number;
  summary?: string;
  issues?: BriefValidationIssue[];
  coverage_statuses?: BriefValidationCoverageStatus[];
  // Finding 4：support_results 不含模型原始 reason，仅保留定位信息。
  support_results?: {
    block: string;
    decision: string;
    evidence_ids: string[];
  }[];
  programmatic_issues?: string[];
  repair_count?: number;
}

export interface KnowledgeBriefAttemptStepOutput {
  decision?: string;
  reason_code?: string;
  unsupported_fragments?: string[];
  explanation?: string;
  suggested_rewrite?: string;
  [key: string]: unknown;
}

// KBR-08：Brief Attempt 的可选过程步骤。后端尚未返回时，前端只展示已有 Attempt 摘要和空状态。
export interface KnowledgeBriefAttemptStep {
  id?: number;
  attempt_id?: number;
  sequence: number;
  iteration?: number;
  phase: string;
  status: string;
  block_path?: string | null;
  provider_id?: string | null;
  provider_model?: string | null;
  prompt_version?: string | null;
  schema_version?: number | null;
  decision?: string | null;
  reason_code?: string | null;
  evidence_ids?: string[];
  output?: KnowledgeBriefAttemptStepOutput | null;
  created_at?: string | null;
  latency_ms?: number | null;
  token_input_count?: number | null;
  token_output_count?: number | null;
  retry_count?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  unsupported_fragments?: string[];
  explanation?: string | null;
  suggested_rewrite?: string | null;
}

export interface KnowledgeSourceBrief {
  id: number;
  source_id: number;
  snapshot_id: number;
  winning_attempt_id: number;
  schema_version: number;
  language: string;
  payload: BriefPayload;
  outdated: boolean;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeBriefAttempt {
  id: number;
  source_id: number;
  snapshot_id: number;
  status: string;
  provider_id: string;
  provider_model: string;
  context_window: number;
  max_output_tokens: number;
  prompt_version: string;
  schema_version: number;
  language: string;
  candidate_payload: BriefPayload | null;
  validation_report: BriefValidationReport;
  error_code: string;
  error_message: string;
  repair_count: number;
  // KI-10：fallback 候选、实际成功 Provider、Provider 层重试进度。
  fallback_provider_id: string;
  fallback_provider_model: string;
  actual_provider_id: string;
  actual_provider_model: string;
  provider_retry_count: number;
  next_retry_at: string | null;
  token_input_count: number;
  token_output_count: number;
  latency_ms: number;
  // 后端逐步提供过程记录；旧 Attempt 不含该字段。
  steps?: KnowledgeBriefAttemptStep[];
  total_steps?: number;
  has_more?: boolean;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeSourceBriefResponse {
  source_id: number;
  brief_status: string;
  brief_block_reason: string;
  brief_error_code: string;
  brief_error_message: string;
  brief: KnowledgeSourceBrief | null;
  latest_attempt: KnowledgeBriefAttempt | null;
  attempts: KnowledgeBriefAttempt[];
}

export interface KnowledgeBriefRebuildResponse {
  source_id: number;
  brief_status: string;
  brief_block_reason: string;
  brief_error_code: string;
  brief_error_message: string;
  status: string;
}
