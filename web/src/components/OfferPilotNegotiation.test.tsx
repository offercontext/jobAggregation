// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ContextPanel from './ChatPanel/ContextPanel';
import type { Offer } from '@/types/offer';

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const offer: Offer = {
  id: 42,
  application_id: 7,
  company_name: '星云数据',
  position_name: '后端工程师',
  status: 'pending',
  base_monthly: 28000,
  months_per_year: 12,
  signing_bonus: 0,
  equity: '',
  perks: '',
  deadline: '',
  notes: '',
  assessment: '',
  total_cash: 336000,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function render(
  offerValue: Offer | null,
  onPrepareOfferNegotiation: (value: Offer) => void,
  offers: Offer[] = [offer],
  contextKey = 'conversation:one',
) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root: Root = createRoot(host);
  act(() => {
    root.render(
      <ContextPanel
        isNego
        offer={offerValue}
        capabilities={[]}
        evidence={[]}
        autoApprove={false}
        hasKey
        degraded={false}
        disabled={false}
        onCapability={vi.fn()}
        onToggleAutoApprove={vi.fn()}
        onPrepareOfferNegotiation={onPrepareOfferNegotiation}
        offers={offers}
        contextKey={contextKey}
      />,
    );
  });
  return { host, root };
}

describe('Pilot offer negotiation entry', () => {
  let root: Root | undefined;
  let host: HTMLDivElement | undefined;

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = undefined;
    host = undefined;
  });

  it('opens an explicit Offer selector without chat or provider work before selection', () => {
    const onPrepare = vi.fn();
    ({ host, root } = render(null, onPrepare));

    const choose = host.querySelector<HTMLButtonElement>('[data-testid="pilot-choose-offer-negotiation"]');
    expect(choose).not.toBeNull();
    act(() => choose?.click());
    const radio = host.querySelector<HTMLInputElement>(`input[value="${offer.id}"]`);
    expect(radio).not.toBeNull();
    act(() => radio?.click());
    expect(onPrepare).not.toHaveBeenCalled();
    expect(host.textContent).toContain('已选择 Offer');
    const continueButton = host.querySelector<HTMLButtonElement>('[data-action="continue-offer-negotiation"]');
    act(() => continueButton?.click());
    expect(onPrepare).not.toHaveBeenCalled();
    const prepare = host.querySelector<HTMLButtonElement>('[data-testid="pilot-prepare-offer-negotiation"]');
    act(() => prepare?.click());
    expect(onPrepare).toHaveBeenCalledTimes(1);
    expect(onPrepare).toHaveBeenCalledWith(offer);
    expect(host.textContent).not.toContain('provider');
  });

  it('opens the shared Offer flow only after an explicitly selected Offer is present', () => {
    const onPrepare = vi.fn();
    ({ host, root } = render(offer, onPrepare));

    const button = host.querySelector<HTMLButtonElement>('[data-testid="pilot-prepare-offer-negotiation"]');
    expect(button).not.toBeNull();
    act(() => button?.click());
    expect(onPrepare).toHaveBeenCalledTimes(1);
    expect(onPrepare).toHaveBeenCalledWith(offer);
  });

  it('keeps an active historical unbound Offer read-only', () => {
    const onPrepare = vi.fn();
    const unbound = { ...offer, application_id: undefined };
    ({ host, root } = render(unbound, onPrepare, [unbound]));

    expect(host.querySelector('[data-testid="pilot-prepare-offer-negotiation"]')).toBeNull();
    expect(host.textContent).toContain('历史未绑定 Offer 仅支持只读查看');
    expect(onPrepare).not.toHaveBeenCalled();
  });

  it('does not open a selector when only historical unbound Offers exist', () => {
    const onPrepare = vi.fn();
    const unbound = { ...offer, application_id: -1 };
    ({ host, root } = render(null, onPrepare, [unbound]));

    expect(host.querySelector('[data-testid="pilot-choose-offer-negotiation"]')).toBeNull();
    expect(host.textContent).toContain('暂无可用于谈薪准备的已绑定 Offer');
    expect(onPrepare).not.toHaveBeenCalled();
  });

  it('clears a selected Offer when the conversation context changes without an explicit Offer', () => {
    const onPrepare = vi.fn();
    ({ host, root } = render(null, onPrepare, [offer], 'conversation:one'));

    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="pilot-choose-offer-negotiation"]')?.click());
    act(() => host?.querySelector<HTMLInputElement>(`input[value="${offer.id}"]`)?.click());
    act(() => host?.querySelector<HTMLButtonElement>('[data-action="continue-offer-negotiation"]')?.click());
    expect(host?.querySelector('[data-testid="pilot-prepare-offer-negotiation"]')).not.toBeNull();

    act(() => {
      root?.render(
        <ContextPanel
          isNego
          offer={null}
          capabilities={[]}
          evidence={[]}
          autoApprove={false}
          hasKey
          degraded={false}
          disabled={false}
          onCapability={vi.fn()}
          onToggleAutoApprove={vi.fn()}
          onPrepareOfferNegotiation={onPrepare}
          offers={[offer]}
          contextKey="conversation:two"
        />,
      );
    });

    expect(host?.querySelector('[data-testid="pilot-prepare-offer-negotiation"]')).toBeNull();
    expect(host?.querySelector('[data-testid="pilot-choose-offer-negotiation"]')).not.toBeNull();
  });

  it('offers a way to replace the selected Offer before preparing', () => {
    const onPrepare = vi.fn();
    ({ host, root } = render(null, onPrepare));

    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="pilot-choose-offer-negotiation"]')?.click());
    act(() => host?.querySelector<HTMLInputElement>(`input[value="${offer.id}"]`)?.click());
    act(() => host?.querySelector<HTMLButtonElement>('[data-action="continue-offer-negotiation"]')?.click());
    act(() => host?.querySelector<HTMLButtonElement>('[data-action="change-offer-negotiation"]')?.click());

    expect(host?.querySelector('[data-testid="pilot-choose-offer-negotiation"]')).toBeNull();
    expect(host?.querySelector('[data-action="continue-offer-negotiation"]')).not.toBeNull();
  });

  it('clears a selected Offer when the Pilot context changes', () => {
    const onPrepare = vi.fn();
    const otherOffer = { ...offer, id: 99, company_name: '远山科技' };
    ({ host, root } = render(null, onPrepare));

    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="pilot-choose-offer-negotiation"]')?.click());
    act(() => host?.querySelector<HTMLInputElement>(`input[value="${offer.id}"]`)?.click());
    act(() => host?.querySelector<HTMLButtonElement>('[data-action="continue-offer-negotiation"]')?.click());
    expect(host?.textContent).toContain('已选择 Offer');

    act(() => {
      root?.render(
        <ContextPanel
          isNego
          offer={otherOffer}
          capabilities={[]}
          evidence={[]}
          autoApprove={false}
          hasKey
          degraded={false}
          disabled={false}
          onCapability={vi.fn()}
          onToggleAutoApprove={vi.fn()}
          onPrepareOfferNegotiation={onPrepare}
          offers={[otherOffer]}
        />,
      );
    });

    expect(host?.textContent).not.toContain('已选择 Offer');
    expect(host?.textContent).toContain('远山科技');
  });
});
