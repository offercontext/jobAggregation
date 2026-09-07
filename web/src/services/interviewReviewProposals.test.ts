import axios, { type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { adapter, createApiClient } = vi.hoisted(() => ({
  adapter: vi.fn<AxiosAdapter>(),
  createApiClient: vi.fn(),
}));

const client = axios.create({ adapter, baseURL: '/api', timeout: 10000 });
vi.mock('./http', () => ({ createApiClient }));
createApiClient.mockReturnValue(client);

const { createInterviewReviewProposal, listInterviewReviewProposals } = await import(
  './interviewReviewProposals'
);

function response(config: InternalAxiosRequestConfig, data: unknown) {
  return { config, data, headers: {}, status: 200, statusText: 'OK' };
}

beforeEach(() => {
  adapter.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('interview review proposal service', () => {
  it('allows an 11-second proposal response through the 130-second Axios request window', async () => {
    vi.useFakeTimers();
    adapter.mockImplementation(
      (config) =>
        new Promise((resolve) => {
          setTimeout(() => resolve(response(config, { id: 3 })), 11000);
        }),
    );

    let settled = false;
    const proposal = createInterviewReviewProposal(7, 'attempt-1').then((value) => {
      settled = true;
      return value;
    });
    await vi.advanceTimersByTimeAsync(0);

    expect(createApiClient).toHaveBeenCalledTimes(1);
    expect(createApiClient).toHaveBeenCalledWith({ baseURL: '/api', timeout: 10000 });
    expect(adapter).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'post',
        timeout: 130000,
        url: '/notes/7/interview-review-proposals',
      }),
    );
    expect(settled).toBe(false);

    await vi.advanceTimersByTimeAsync(11000);
    await expect(proposal).resolves.toEqual({ id: 3 });
    expect(adapter).toHaveBeenCalledTimes(1);
  });

  it('keeps proposal reads on the 10-second Axios client default', async () => {
    adapter.mockImplementation(async (config) => response(config, []));

    await expect(listInterviewReviewProposals(7)).resolves.toEqual([]);

    expect(adapter).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'get',
        timeout: 10000,
        url: '/notes/7/interview-review-proposals',
      }),
    );
  });

  it.each([
    ['interview_review_event_required', '请先绑定有效的面试事件。'],
    ['interview_review_not_found', '面试复盘已不可见，请重新打开投递。'],
    ['interview_review_source_conflict', '复盘来源已变化，请重新核对后再生成。'],
    ['interview_review_provider_error', 'AI 服务暂不可用，请稍后重试。'],
    ['interview_review_unverifiable', 'AI 建议未通过证据校验，原复盘未受影响，请重试。'],
  ])('maps %s without exposing server text', async (code, message) => {
    adapter.mockRejectedValue({
      response: { status: 502, data: { error_code: code, error: 'secret server detail' } },
      message: 'Axios secret',
    });

    await expect(createInterviewReviewProposal(7, 'attempt-1')).rejects.toMatchObject({ message });
    await expect(createInterviewReviewProposal(7, 'attempt-1')).rejects.not.toThrow('secret');
  });

  it('uses a neutral fallback for unknown failures', async () => {
    adapter.mockRejectedValue(new Error('raw internal error'));

    await expect(createInterviewReviewProposal(7, 'attempt-1')).rejects.toMatchObject({
      message: '复盘建议暂时不可用，请稍后重试。',
    });
  });

  it('keeps a bare 502 distinguishable as an unknown result', async () => {
    adapter.mockRejectedValue({
      response: { status: 502, data: { error: 'provider detail' } },
      message: 'Axios provider detail',
    });

    const error = await createInterviewReviewProposal(7, 'attempt-1').catch((cause) => cause);

    expect(error).toMatchObject({ message: 'AI 服务暂不可用，请稍后重试。' });
    expect(error).toMatchObject({ code: undefined });
  });

  it('keeps the original idempotency key and does not retry a failed creation', async () => {
    adapter.mockRejectedValue({ response: { status: 502, data: {} } });

    await expect(createInterviewReviewProposal(7, 'original-key')).rejects.toMatchObject({
      message: 'AI 服务暂不可用，请稍后重试。',
    });

    expect(adapter).toHaveBeenCalledTimes(1);
    const request = adapter.mock.calls[0]?.[0];
    expect(request).toMatchObject({ method: 'post', timeout: 130000 });
    expect(JSON.parse(request?.data as string)).toEqual({ idempotency_key: 'original-key' });
  });
});
