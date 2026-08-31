import { beforeEach, describe, expect, it, vi } from 'vitest';

const { apiGet, apiPost, createApiClient } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  createApiClient: vi.fn(),
}));

vi.mock('./http', () => ({ createApiClient }));
createApiClient.mockReturnValue({ get: apiGet, post: apiPost });

const service = await import('./interviewStories');

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
});

describe('interview story service', () => {
  it('uses the UI and Pilot proposal endpoints without chat APIs', async () => {
    const input = {
      target_story_id: null,
      expected_current_version_id: null,
      expected_story_revision: null,
      selections: [],
      assertions: [],
      idempotency_key: 'story-service-key-0001',
    };
    apiPost.mockResolvedValue({ data: { id: 7, attempt_status: 'generating', generation_revision: 1, source_fingerprint: 'fp', retry_after_ms: 1000 } });

    await service.createInterviewStoryProposal(input);
    await service.createInterviewStoryProposal(input, 'pilot');

    expect(apiPost).toHaveBeenNthCalledWith(1, '/interview-story-proposals', input);
    expect(apiPost).toHaveBeenNthCalledWith(2, '/pilot/interview-story-proposals', input);
  });

  it('uses only Story APIs for archive and version history reads', async () => {
    apiGet.mockResolvedValue({ data: [] });
    apiPost.mockResolvedValue({ data: { id: 5, status: 'archived' } });

    await service.listInterviewStoryVersions(5);
    await service.archiveInterviewStory(5, 2);

    expect(apiGet).toHaveBeenCalledWith('/interview-stories/5/versions');
    expect(apiPost).toHaveBeenCalledWith('/interview-stories/5/archive', { expected_story_revision: 2 });
  });

  it('reads explicit Story source candidates without any write request', async () => {
    apiGet.mockResolvedValue({ data: { resumes: [], interview_notes: [], mock_turns: [] } });

    await service.listInterviewStorySourceCandidates();
    await service.listInterviewStorySourceCandidates(9);

    expect(apiGet).toHaveBeenNthCalledWith(1, '/interview-story-sources', { params: undefined });
    expect(apiGet).toHaveBeenNthCalledWith(2, '/interview-story-sources', { params: { review_note_id: 9 } });
    expect(apiPost).not.toHaveBeenCalled();
  });

  it('sends the user-owned manual idempotency key only to Story save APIs', async () => {
    const input = {
      content: { title: '一次延迟排查', blocks: [], capability_labels: [], applicable_questions: [], fact_gap_codes: ['missing_result'] },
      evidence_links: [], selections: [], assertions: [], expected_current_version_id: null,
      idempotency_key: 'manual-story-service-key-01',
    };
    apiPost.mockResolvedValue({ data: { id: 5 } });

    await service.createInterviewStory(input);

    expect(apiPost).toHaveBeenCalledWith('/interview-stories', input);
  });

  it('keeps the safe provider-unknown Attempt identity from a 502 response', async () => {
    const input = {
      target_story_id: null,
      expected_current_version_id: null,
      expected_story_revision: null,
      selections: [],
      assertions: [],
      idempotency_key: 'story-provider-unknown-key',
    };
    apiPost.mockRejectedValue({
      response: {
        status: 502,
        data: { error_code: 'story_provider_error', id: 44, attempt_status: 'provider_unknown', retry_after_ms: 30_250 },
      },
    });

    await expect(service.createInterviewStoryProposal(input)).rejects.toMatchObject({
      status: 502,
      code: 'story_provider_error',
      attemptId: 44,
      retryAfterMs: 30_250,
    });
  });

  it('requests Story N+1 with both exact generation counters', async () => {
    const response = {
      schema_version: 1,
      contract: 'story_product_action_proposal_response_v1',
      operation_id: 'operation-2',
      action_call_id: 'call-2',
      product_action_generation: 2,
      status: 'proposed',
      proposal_created: true,
      confirmation_token: 'server-token-2',
    };
    apiPost.mockResolvedValue({ data: response });
    await expect(service.createInterviewStoryProductAction(44, {
      expected_generation_revision: 3,
      expected_product_action_generation: 1,
    })).resolves.toEqual(response);
    expect(apiPost).toHaveBeenCalledWith('/interview-story-proposals/44/product-actions', {
      expected_generation_revision: 3,
      expected_product_action_generation: 1,
    });
    apiPost.mockResolvedValue({ data: { ...response, leaked: 'nope' } });
    await expect(service.createInterviewStoryProductAction(44, {
      expected_generation_revision: 3,
      expected_product_action_generation: 1,
    })).rejects.toMatchObject({ code: 'story_invalid_response' });
  });

  it('decodes the Story N+1 response as a closed proposed-or-terminal union', async () => {
    const base = {
      schema_version: 1, contract: 'story_product_action_proposal_response_v1',
      operation_id: 'operation-2', action_call_id: 'call-2', product_action_generation: 2,
    };
    const invalid = [
      { ...base, status: 'proposed', proposal_created: true, confirmation_token: 'server-token-2', terminal_result: {} },
      { ...base, status: 'committed', proposal_created: false },
      { ...base, status: 'rejected', proposal_created: true, terminal_result: {} },
      { ...base, status: 'failed', proposal_created: false, confirmation_token: 'leaked', terminal_result: {} },
    ];
    for (const response of invalid) {
      apiPost.mockResolvedValueOnce({ data: response });
      await expect(service.createInterviewStoryProductAction(44, {
        expected_generation_revision: 3,
        expected_product_action_generation: 1,
      })).rejects.toMatchObject({ code: 'story_invalid_response' });
    }
    const terminal = { ...base, status: 'committed', proposal_created: false, terminal_result: { story_id: 8, version_id: 12 } };
    apiPost.mockResolvedValueOnce({ data: terminal });
    await expect(service.createInterviewStoryProductAction(44, {
      expected_generation_revision: 3,
      expected_product_action_generation: 1,
    })).resolves.toEqual(terminal);
    const replayedProposal = { ...base, status: 'proposed', proposal_created: false, confirmation_token: 'server-token-2' };
    apiPost.mockResolvedValueOnce({ data: replayedProposal });
    await expect(service.createInterviewStoryProductAction(44, {
      expected_generation_revision: 3,
      expected_product_action_generation: 1,
    })).resolves.toEqual(replayedProposal);
  });
});
