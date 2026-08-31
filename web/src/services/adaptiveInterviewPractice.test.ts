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
});
