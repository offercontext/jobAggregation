import { beforeEach, describe, expect, it, vi } from 'vitest';

const { post, createApiClient } = vi.hoisted(() => ({ post: vi.fn(), createApiClient: vi.fn() }));
vi.mock('./http', () => ({ createApiClient }));
createApiClient.mockReturnValue({ post, get: vi.fn() });
const service = await import('./interviewPreparationProposals');

const base = {
  application_id: 7,
  event_id: 11,
  resume_id: 13,
  jd_version_id: 17,
  knowledge_selections: [],
  user_assertions: [],
  idempotency_key: '00000000-0000-0000-0000-000000000001',
};

beforeEach(() => { post.mockReset().mockResolvedValue({ data: { attempt_status: 'generating' } }); });

describe('interview preparation proposal service', () => {
  it('genuinely omits the readiness field for V1', async () => {
    await service.createInterviewPreparationProposal(base);
    const body = post.mock.calls[0]?.[1];
    expect(post).toHaveBeenCalledWith('/applications/7/interview-preparation-proposals', body);
    expect(Object.prototype.hasOwnProperty.call(body, 'readiness_feedback_version_ids')).toBe(false);
  });

  it('preserves an explicit empty V2 selection', async () => {
    await service.createInterviewPreparationProposal({ ...base, readiness_feedback_version_ids: [] });
    expect(post.mock.calls[0]?.[1]).toEqual({
      event_id: 11,
      resume_id: 13,
      jd_version_id: 17,
      knowledge_selections: [],
      user_assertions: [],
      idempotency_key: '00000000-0000-0000-0000-000000000001',
      readiness_feedback_version_ids: [],
    });
  });
});
