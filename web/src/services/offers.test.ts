import { afterEach, describe, expect, it, vi } from 'vitest';

const { aiHttp, createClientMock, normalHttp } = vi.hoisted(() => ({
  aiHttp: {
    delete: vi.fn(),
    get: vi.fn(),
    patch: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
  createClientMock: vi.fn(),
  normalHttp: {
    delete: vi.fn(),
    get: vi.fn(),
    patch: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}));

vi.mock('./http', () => ({
  createApiClient: (options: { timeout: number }) => {
    createClientMock(options);
    return options.timeout === 130000 ? aiHttp : normalHttp;
  },
}));

import {
  confirmOfferNegotiationProposal,
  createOffer,
  createOfferNegotiationProposal,
  previewOfferNegotiation,
} from './offers';

afterEach(() => {
  aiHttp.post.mockReset();
  normalHttp.post.mockReset();
});

describe('Offer request timeout boundaries', () => {
  it('allows synchronous AI negotiation generation to finish within the established AI window', async () => {
    aiHttp.post.mockResolvedValue({ data: { id: 17, attempt_status: 'ready' } });

    await createOfferNegotiationProposal(11, {
      idempotency_key: 'offer-negotiation-key-0001',
      dimension_ids: [3],
      goal: '确认薪资结构',
      concerns: '固定薪资偏低',
      scenario: '电话沟通',
    });

    expect(createClientMock).toHaveBeenCalledWith({ baseURL: '/api', timeout: 130000 });
    expect(aiHttp.post).toHaveBeenCalledWith(
      '/offers/11/negotiation/proposals',
      expect.objectContaining({ idempotency_key: 'offer-negotiation-key-0001' }),
      { headers: { 'X-OfferPilot-Entrypoint': 'ui' } },
    );
    expect(normalHttp.post).not.toHaveBeenCalled();
  });

  it('keeps ordinary Offer writes on the short request timeout client', async () => {
    normalHttp.post.mockResolvedValue({ data: { id: 11 } });

    await createOffer({
      application_id: 269,
      company_name: '去哪儿旅行',
      position_name: 'Agent 开发',
      status: 'pending',
    });

    expect(normalHttp.post).toHaveBeenCalledWith('/offers', expect.objectContaining({ application_id: 269 }));
    expect(aiHttp.post).not.toHaveBeenCalled();
  });

  it('keeps the read-only negotiation preview on the short request timeout client', async () => {
    normalHttp.post.mockResolvedValue({ data: { source_fingerprint: 'source', snapshot: {} } });

    await previewOfferNegotiation(11, {
      dimension_ids: [],
      goal: '确认薪资结构',
      concerns: '固定薪资偏低',
      scenario: '电话沟通',
    });

    expect(normalHttp.post).toHaveBeenCalledWith(
      '/offers/11/negotiation/preview',
      expect.objectContaining({ goal: '确认薪资结构' }),
    );
    expect(aiHttp.post).not.toHaveBeenCalled();
  });

  it('keeps the negotiation save mutation on the short request timeout client', async () => {
    normalHttp.post.mockResolvedValue({ data: { id: 21, proposal_id: 17 } });

    await confirmOfferNegotiationProposal(17, {
      confirmation_key: 'offer-negotiation-confirm-key-0001',
      selected_blocks: ['goal-1'],
      edited_content: {},
    });

    expect(normalHttp.post).toHaveBeenCalledWith(
      '/offer-negotiation/proposals/17/confirm',
      expect.objectContaining({ confirmation_key: 'offer-negotiation-confirm-key-0001' }),
      { headers: { 'X-OfferPilot-Entrypoint': 'ui' } },
    );
    expect(aiHttp.post).not.toHaveBeenCalled();
  });
});
