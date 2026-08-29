// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Offer } from '@/types/offer';
import PilotOfferSelectionCard from './PilotOfferSelectionCard';

const offer1: Offer = {
  id: 1, application_id: 11, company_name: '星云数据', position_name: '后端工程师', status: 'pending',
  base_monthly: 28000, months_per_year: 12, signing_bonus: 0, equity: '', perks: '',
  deadline: '', notes: '', assessment: '', total_cash: 336000,
  created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z',
};
const offer2 = { ...offer1, id: 2, company_name: '远山科技', position_name: '平台工程师' };

describe('PilotOfferSelectionCard', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;
  });

  it('asks for an explicit Offer and confirms the user answer before continuing', () => {
    const onContinue = vi.fn();
    const onCancel = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<PilotOfferSelectionCard offers={[offer1, offer2]} onContinue={onContinue} onCancel={onCancel} />));

    expect(host.textContent).toContain('选择要准备谈薪的 Offer');
    act(() => host?.querySelector<HTMLInputElement>('[value="2"]')?.click());
    expect(host.textContent).toContain('已选择');
    expect(host.textContent).toContain('远山科技');
    expect(onContinue).not.toHaveBeenCalled();

    act(() => host?.querySelector<HTMLButtonElement>('[data-action="continue-offer-negotiation"]')?.click());
    expect(onContinue).toHaveBeenCalledWith(offer2);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('shows a safe empty state when there are no Offers', () => {
    const onContinue = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<PilotOfferSelectionCard offers={[]} onContinue={onContinue} onCancel={vi.fn()} />));
    expect(host.textContent).toContain('暂无可用于谈薪准备的已绑定 Offer');
    expect(host.querySelector('[data-action="continue-offer-negotiation"]')).toBeNull();
  });

  it('filters historical unbound Offers before selection', () => {
    const onContinue = vi.fn();
    const unbound = { ...offer1, id: 3, application_id: undefined, company_name: '历史公司' };
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<PilotOfferSelectionCard offers={[unbound, offer2]} onContinue={onContinue} onCancel={vi.fn()} />));

    expect(host.textContent).not.toContain('历史公司');
    expect(host.querySelector<HTMLInputElement>('[value="3"]')).toBeNull();
    act(() => host?.querySelector<HTMLInputElement>('[value="2"]')?.click());
    act(() => host?.querySelector<HTMLButtonElement>('[data-action="continue-offer-negotiation"]')?.click());
    expect(onContinue).toHaveBeenCalledWith(offer2);
  });

  it('shows a safe empty state when every Offer is historical and unbound', () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(
      <PilotOfferSelectionCard
        offers={[{ ...offer1, application_id: 0 }, { ...offer2, application_id: -1 }]}
        onContinue={vi.fn()}
        onCancel={vi.fn()}
      />,
    ));
    expect(host.textContent).toContain('暂无可用于谈薪准备的已绑定 Offer');
    expect(host.querySelector('[data-action="continue-offer-negotiation"]')).toBeNull();
  });
});
