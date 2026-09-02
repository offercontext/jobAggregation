import { describe, expect, it } from 'vitest';
import { buildOfferNegotiationPilotDraft } from './pilotHandoff';
import type { Offer } from '@/types/offer';

const offer: Offer = {
  id: 17,
  application_id: 42,
  company_name: '去哪儿旅行',
  position_name: 'Agent 开发',
  status: 'pending',
  base_monthly: 24_000,
  months_per_year: 16,
  signing_bonus: 0,
  equity: '',
  perks: '',
  deadline: '',
  notes: '',
  assessment: '',
  total_cash: 384_000,
  created_at: '2026-09-02T00:00:00Z',
  updated_at: '2026-09-02T00:00:00Z',
};

describe('Offer negotiation Pilot handoff', () => {
  it('builds an editable user draft from the visible Offer and current brief without internal ids', () => {
    const draft = buildOfferNegotiationPilotDraft(offer, {
      goal: ' 希望固定月薪多 2K ',
      concerns: '担心对方取消 Offer',
      scenario: 'HR 电话沟通',
    });

    expect(draft).toContain('去哪儿旅行 · Agent 开发');
    expect(draft).toContain('我的目标：希望固定月薪多 2K');
    expect(draft).toContain('我的顾虑：担心对方取消 Offer');
    expect(draft).toContain('沟通场景：HR 电话沟通');
    expect(draft).toContain('消息发送前');
    expect(draft).not.toContain('17');
    expect(draft).not.toContain('42');
  });

  it('omits blank brief rows instead of inventing facts', () => {
    const draft = buildOfferNegotiationPilotDraft(offer, {
      goal: ' ',
      concerns: '',
      scenario: '\t',
    });

    expect(draft).toContain('帮我一起梳理谈薪目标、底线和沟通方式');
    expect(draft).not.toContain('我的目标：');
    expect(draft).not.toContain('我的顾虑：');
    expect(draft).not.toContain('沟通场景：');
  });
});
