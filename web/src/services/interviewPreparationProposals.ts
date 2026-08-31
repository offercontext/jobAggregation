import axios from 'axios';
import type {
  CreateInterviewPreparationProposalInput,
  InterviewPreparationProposal,
  InterviewPreparationPendingResponse,
} from '@/types/interviewPreparationProposal';
import { InterviewPreparationProposalError } from '@/types/interviewPreparationProposal';
import { createApiClient } from './http';

export { InterviewPreparationProposalError } from '@/types/interviewPreparationProposal';

const http = createApiClient({ baseURL: '/api', timeout: 130000 });

export async function createInterviewPreparationProposal(
  input: CreateInterviewPreparationProposalInput,
): Promise<InterviewPreparationProposal | InterviewPreparationPendingResponse> {
  try {
    const { application_id, readiness_feedback_version_ids, ...legacyBody } = input;
    const body = Object.prototype.hasOwnProperty.call(input, 'readiness_feedback_version_ids')
      ? { ...legacyBody, readiness_feedback_version_ids }
      : legacyBody;
    const response = await http.post<InterviewPreparationProposal | InterviewPreparationPendingResponse>(
      `/applications/${application_id}/interview-preparation-proposals`,
      body,
    );
    return response.data;
  } catch (error) {
    if (axios.isAxiosError(error)) {
      const status = error.response?.status ?? 0;
      const data = error.response?.data as { error_code?: unknown } | undefined;
      throw new InterviewPreparationProposalError(status, typeof data?.error_code === 'string' ? data.error_code : null);
    }
    throw new InterviewPreparationProposalError(0, null);
  }
}

export async function listInterviewPreparationProposals(
  applicationId: number,
): Promise<InterviewPreparationProposal[]> {
  const { data } = await http.get<InterviewPreparationProposal[]>(
    `/applications/${applicationId}/interview-preparation-proposals`,
  );
  return data;
}

export async function getInterviewPreparationProposal(
  applicationId: number,
  proposalId: number,
): Promise<InterviewPreparationProposal> {
  const { data } = await http.get<InterviewPreparationProposal>(
    `/applications/${applicationId}/interview-preparation-proposals/${proposalId}`,
  );
  return data;
}
