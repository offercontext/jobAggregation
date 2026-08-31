import axios from 'axios';
import type {
  InterviewStory,
  InterviewStoryClientEvidenceLink,
  InterviewStoryManualInput,
  InterviewStoryPendingAttempt,
  InterviewStoryNextProductActionResponse,
  InterviewStoryProposalAttempt,
  InterviewStoryProposalInput,
  InterviewStorySourceCandidates,
  InterviewStoryVersion,
} from '@/types/interviewStory';
import { InterviewStoryError as StoryError } from '@/types/interviewStory';
import { createApiClient } from './http';

export { InterviewStoryError } from '@/types/interviewStory';

const http = createApiClient({ baseURL: '/api', timeout: 130000 });

type ProposalResponse = InterviewStoryProposalAttempt | InterviewStoryPendingAttempt;

function parseNextProductAction(value: unknown): InterviewStoryNextProductActionResponse {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new StoryError(0, 'story_invalid_response');
  const record = value as Record<string, unknown>;
  const required = ['schema_version', 'contract', 'operation_id', 'action_call_id', 'product_action_generation', 'status', 'proposal_created'];
  const allowed = new Set([...required, 'confirmation_token', 'terminal_result']);
  if (required.some((key) => !(key in record)) || Object.keys(record).some((key) => !allowed.has(key))) {
    throw new StoryError(0, 'story_invalid_response');
  }
  const status = record.status;
  const validStatus = status === 'proposed' || status === 'rejected' || status === 'committed' || status === 'failed';
  const token = record.confirmation_token;
  const terminalResult = record.terminal_result;
  const closedUnionValid = status === 'proposed'
    ? typeof record.proposal_created === 'boolean' && typeof token === 'string' && token.length > 0 && terminalResult === undefined
    : record.proposal_created === false && token === undefined
      && typeof terminalResult === 'object' && terminalResult !== null && !Array.isArray(terminalResult);
  if (
    record.schema_version !== 1
    || record.contract !== 'story_product_action_proposal_response_v1'
    || typeof record.operation_id !== 'string'
    || typeof record.action_call_id !== 'string'
    || typeof record.product_action_generation !== 'number'
    || !Number.isSafeInteger(record.product_action_generation)
    || record.product_action_generation < 1
    || !validStatus
    || !closedUnionValid
  ) throw new StoryError(0, 'story_invalid_response');
  return record as unknown as InterviewStoryNextProductActionResponse;
}

function toStoryError(error: unknown): StoryError {
  const response = axios.isAxiosError(error)
    ? error.response
    : (error as { response?: { status?: number; data?: { error_code?: unknown; id?: unknown; retry_after_ms?: unknown } } } | null)?.response;
  const code = typeof response?.data?.error_code === 'string' ? response.data.error_code : null;
  const rawAttemptId = response?.data?.id;
  const attemptId = typeof rawAttemptId === 'number' && Number.isSafeInteger(rawAttemptId) && rawAttemptId > 0
    ? rawAttemptId
    : null;
  const rawRetryAfterMs = response?.data?.retry_after_ms;
  const retryAfterMs = typeof rawRetryAfterMs === 'number' && Number.isSafeInteger(rawRetryAfterMs) && rawRetryAfterMs >= 0
    ? rawRetryAfterMs
    : null;
  return new StoryError(response?.status ?? 0, code, attemptId, retryAfterMs);
}

async function request<T>(operation: () => Promise<{ data: T }>): Promise<T> {
  try {
    return (await operation()).data;
  } catch (error) {
    throw toStoryError(error);
  }
}

export function listInterviewStories(status: 'active' | 'archived' = 'active', query = ''): Promise<InterviewStory[]> {
  return request(() => http.get<InterviewStory[]>('/interview-stories', { params: { status, query } }));
}

export function getInterviewStory(storyId: number): Promise<InterviewStory> {
  return request(() => http.get<InterviewStory>(`/interview-stories/${storyId}`));
}

export function listInterviewStorySourceCandidates(reviewNoteId?: number): Promise<InterviewStorySourceCandidates> {
  return request(() => http.get<InterviewStorySourceCandidates>('/interview-story-sources', {
    params: reviewNoteId ? { review_note_id: reviewNoteId } : undefined,
  }));
}

export function listInterviewStoryVersions(storyId: number): Promise<Array<Pick<InterviewStoryVersion, 'id' | 'version_number' | 'origin_kind' | 'confirmed_at' | 'source_fingerprint'>>> {
  return request(() => http.get(`/interview-stories/${storyId}/versions`));
}

export function getInterviewStoryVersion(storyId: number, versionId: number): Promise<InterviewStoryVersion> {
  return request(() => http.get<InterviewStoryVersion>(`/interview-stories/${storyId}/versions/${versionId}`));
}

export function createInterviewStory(input: InterviewStoryManualInput): Promise<InterviewStory> {
  return request(() => http.post<InterviewStory>('/interview-stories', input));
}

export function createInterviewStoryVersion(
  storyId: number,
  input: InterviewStoryManualInput & { expected_story_revision: number },
): Promise<InterviewStory> {
  return request(() => http.post<InterviewStory>(`/interview-stories/${storyId}/versions`, input));
}

export function archiveInterviewStory(storyId: number, expectedStoryRevision: number): Promise<InterviewStory> {
  return request(() => http.post<InterviewStory>(`/interview-stories/${storyId}/archive`, { expected_story_revision: expectedStoryRevision }));
}

export function restoreInterviewStory(storyId: number, expectedStoryRevision: number): Promise<InterviewStory> {
  return request(() => http.post<InterviewStory>(`/interview-stories/${storyId}/restore`, { expected_story_revision: expectedStoryRevision }));
}

export function createInterviewStoryProposal(
  input: InterviewStoryProposalInput,
  entrypoint: 'ui' | 'pilot' = 'ui',
): Promise<ProposalResponse> {
  const endpoint = entrypoint === 'pilot' ? '/pilot/interview-story-proposals' : '/interview-story-proposals';
  return request(() => http.post<ProposalResponse>(endpoint, input));
}

export function getInterviewStoryProposal(attemptId: number): Promise<ProposalResponse> {
  return request(() => http.get<ProposalResponse>(`/interview-story-proposals/${attemptId}`));
}

export function createInterviewStoryProductAction(
  attemptId: number,
  input: { expected_generation_revision: number; expected_product_action_generation: number },
): Promise<InterviewStoryNextProductActionResponse> {
  return request(() => http.post<InterviewStoryNextProductActionResponse>(
    `/interview-story-proposals/${attemptId}/product-actions`,
    input,
  )).then(parseNextProductAction);
}

export function confirmInterviewStoryProposal(
  attemptId: number,
  input: {
    confirmation_token: string;
    content: InterviewStoryManualInput['content'];
    evidence_links: InterviewStoryClientEvidenceLink[];
    expected_current_version_id: number | null;
    expected_story_revision: number | null;
  },
): Promise<{ story_id: number; version_id: number; created: boolean }> {
  return request(() => http.post(`/interview-story-proposals/${attemptId}/confirm`, input));
}
