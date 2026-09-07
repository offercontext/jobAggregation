import type { MatchResult } from './ai';

export type ResumeSource = 'manual' | 'dialog' | 'upload' | 'sample' | 'sample_copy';
export type ResumeCreateSource = 'manual' | 'dialog';
export type ResumeSampleID = 'backend' | 'frontend' | 'product';

export interface CareerIntent {
  target_roles?: string[];
  target_locations?: string[];
  [key: string]: unknown;
}

export interface ResumeContent {
  career_intent?: CareerIntent;
  contact?: Record<string, unknown>;
  education?: unknown[];
  experience?: unknown[];
  projects?: unknown[];
  skills?: unknown[] | Record<string, unknown> | string;
  raw_text?: string;
  [key: string]: unknown;
}

// Resume row mirrors the v0.1 API payload while keeping old compatibility fields.
export interface Resume {
  id: number;
  name: string;
  file_path: string;
  parsed_data: string;
  parse_status: string;
  title: string;
  is_master: boolean;
  parent_resume_id: number | null;
  source: ResumeSource;
  source_file_path: string;
  content_json: ResumeContent;
  deleted_at: string | null;
  created_at: string;
  completion_percent: number;
  missing_sections: string[];
  is_complete: boolean;
}

export interface CreateResumeInput {
  title?: string;
  source?: ResumeCreateSource;
  content_json?: ResumeContent;
  career_intent?: CareerIntent;
  text?: string;
  name?: string;
  parsed_data?: string;
  parse_status?: string;
}

export interface CreateResumeFromSampleInput {
  sample_id: ResumeSampleID;
  title?: string;
}

export interface UpdateResumeInput {
  title?: string;
  content_json?: ResumeContent;
  career_intent?: CareerIntent;
  is_master?: boolean;
  source?: ResumeSource;
}

export interface CopyResumeInput {
  title?: string;
}

export interface MatchResumeResponse {
  id: number;
  resume_id: number;
  application_id?: number;
  result: MatchResult;
}

export type ResumeStatus = 'text-ready' | 'parse-failed';

export interface ResumeImportField { path: string; value: string; evidence: string }
export interface ResumeImportPreview { resume_id: number; source_fingerprint: string; fields: ResumeImportField[] }
export interface ConfirmResumeImportInput { source_fingerprint: string; fields: ResumeImportField[] }
