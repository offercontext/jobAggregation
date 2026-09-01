import { beforeEach, describe, expect, it, vi } from 'vitest';

const { post, createApiClient } = vi.hoisted(() => ({ post: vi.fn(), createApiClient: vi.fn() }));
vi.mock('./http', () => ({ createApiClient }));
createApiClient.mockReturnValue({ post, get: vi.fn() });
const service = await import('./adaptiveInterviewPractice');

beforeEach(() => { post.mockReset().mockResolvedValue({ data: { id: 8 } }); });

describe('adaptive interview practice service', () => {
  it('freezes the V2 exact-pair start body without a legacy fallback', async () => {
    const input = {
      readiness_signal_version_id: 91,
      target_application_event_id: 103,
      expected_source_fingerprint: `sha256:${'a'.repeat(64)}`,
      expected_target_fingerprint: `sha256:${'b'.repeat(64)}`,
      idempotency_key: '00000000-0000-0000-0000-000000000001',
    };
    await service.startAdaptivePracticeV2(input);
    expect(post).toHaveBeenCalledWith('/interview-practice/plans', input);
  });

  it('parses the safe error code and HTTP status without exposing response data', async () => {
    const input = {
      readiness_signal_version_id: 91,
      target_application_event_id: 103,
      expected_source_fingerprint: `sha256:${'a'.repeat(64)}`,
      expected_target_fingerprint: `sha256:${'b'.repeat(64)}`,
      idempotency_key: '00000000-0000-0000-0000-000000000001',
    };
    post.mockRejectedValueOnce({
      isAxiosError: true,
      response: {
        status: 503,
        data: { error_code: 'adaptive_practice_unavailable', detail: 'do not expose this response body' },
      },
    });
    let caught: unknown;
    try {
      await service.startAdaptivePracticeV2(input);
    } catch (error) {
      caught = error;
    }
    expect(caught).toMatchObject({ code: 'adaptive_practice_unavailable', status: 503 });
    expect(caught).toBeInstanceOf(service.AdaptivePracticeError);
    expect((caught as Error).message).not.toContain('do not expose this response body');
    expect(caught).not.toHaveProperty('detail');
    expect(caught).not.toHaveProperty('response');
    expect(caught).not.toHaveProperty('body');
  });
});
