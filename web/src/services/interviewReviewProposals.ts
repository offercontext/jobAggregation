import axios from 'axios';
import type { InterviewReviewProposal } from '@/types/interviewReviewProposal';
import { createApiClient } from './http';

const http = createApiClient({ baseURL: '/api', timeout: 10000 });

const SAFE_ERRORS: Record<string, string> = {
  interview_review_event_required: '请先绑定有效的面试事件。',
  interview_review_not_found: '面试复盘已不可见，请重新打开投递。',
  interview_review_source_conflict: '复盘来源已变化，请重新核对后再生成。',
  interview_review_provider_error: 'AI 服务暂不可用，请稍后重试。',
  interview_review_unverifiable: 'AI 建议未通过证据校验，原复盘未受影响，请重试。',
};

export class InterviewReviewProposalError extends Error {
  readonly code?: string;

  constructor(message: string, code?: string) {
    super(message);
    this.name = 'InterviewReviewProposalError';
    this.code = code;
  }
}

function safeError(error: unknown): InterviewReviewProposalError {
  const response = axios.isAxiosError(error)
    ? error.response
    : (error as { response?: { status?: number; data?: unknown } } | null)?.response;
  const data = response?.data as { error_code?: unknown } | undefined;
  const code = typeof data?.error_code === 'string' ? data.error_code : undefined;
  if (code && SAFE_ERRORS[code]) return new InterviewReviewProposalError(SAFE_ERRORS[code], code);

  if (response?.status === 404) {
    return new InterviewReviewProposalError(SAFE_ERRORS.interview_review_not_found, 'interview_review_not_found');
  }
  if (response?.status === 409) {
    return new InterviewReviewProposalError(SAFE_ERRORS.interview_review_source_conflict, 'interview_review_source_conflict');
  }
  if (response?.status === 422) {
    return new InterviewReviewProposalError(SAFE_ERRORS.interview_review_event_required, 'interview_review_event_required');
  }
  if (response?.status === 502) {
    return new InterviewReviewProposalError(SAFE_ERRORS.interview_review_provider_error);
  }
  return new InterviewReviewProposalError('复盘建议暂时不可用，请稍后重试。');
}

export async function listInterviewReviewProposals(noteID: number): Promise<InterviewReviewProposal[]> {
  try {
    const { data } = await http.get<InterviewReviewProposal[]>(
      `/notes/${noteID}/interview-review-proposals`,
    );
    return data;
  } catch (error) {
    throw safeError(error);
  }
}

export async function getInterviewReviewProposal(
  noteID: number,
  proposalID: number,
): Promise<InterviewReviewProposal> {
  try {
    const { data } = await http.get<InterviewReviewProposal>(
      `/notes/${noteID}/interview-review-proposals/${proposalID}`,
    );
    return data;
  } catch (error) {
    throw safeError(error);
  }
}

export async function createInterviewReviewProposal(
  noteID: number,
  idempotencyKey: string,
): Promise<InterviewReviewProposal> {
  try {
    const { data } = await http.post<InterviewReviewProposal>(
      `/notes/${noteID}/interview-review-proposals`,
      { idempotency_key: idempotencyKey },
      { timeout: 130000 },
    );
    return data;
  } catch (error) {
    throw safeError(error);
  }
}
