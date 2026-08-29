// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Offer } from '@/types/offer';
import OfferCard from './OfferCard';

const offer: Offer = {
  id: 7,
  application_id: 42,
  company_name: '星云数据',
  position_name: '后端工程师',
  status: 'pending',
  base_monthly: 28000,
  months_per_year: 12,
  signing_bonus: 0,
  equity: '',
  perks: '补充医疗、弹性办公',
  deadline: '2026-08-15',
  notes: '筱哲｜一线业务平台方向',
  assessment: '',
  total_cash: 336000,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

describe('OfferCard', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;
  });

  it('renders one preparation action and a return-to-application action', () => {
    const onNegotiation = vi.fn();
    const onCoach = vi.fn();
    const onOpenApplication = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);

    act(() => {
      root?.render(
        <OfferCard
          offer={offer}
          selected={false}
          onToggleSelect={vi.fn()}
          onCoach={onCoach}
          onNegotiation={onNegotiation}
          onView={vi.fn()}
          onOpenApplication={onOpenApplication}
        />,
      );
    });

    const prepare = host.querySelector<HTMLButtonElement>('[data-action="start-negotiation"]');
    const coach = host.querySelector<HTMLButtonElement>('[data-action="open-negotiation-coach"]');
    expect(host.querySelector('input[aria-label]')?.getAttribute('aria-label')).toContain('星云数据');
    expect(prepare?.textContent).toContain('准备谈薪');
    expect(prepare?.className).toContain('ant-btn-primary');
    expect(coach).toBeNull();
    expect(host.textContent).toContain('星云数据');
    expect(host.textContent).toContain('后端工程师');
    expect(host.textContent).toContain('28K');
    expect(host.textContent).toContain('签字费 0.0万');
    expect(host.textContent).not.toContain('签字费 无');
    expect(host.textContent).toContain('截止 2026-08-15');
    expect(host.querySelector('[data-action="view-offer"]')).not.toBeNull();

    act(() => prepare?.click());
    expect(onNegotiation).toHaveBeenCalledWith(offer);
    expect(onCoach).not.toHaveBeenCalled();
    const back = host.querySelector<HTMLButtonElement>('[data-action="open-application"]');
    expect(back).not.toBeNull();
    act(() => back?.click());
    expect(onOpenApplication).toHaveBeenCalledWith(42);
  });

  it('keeps one preparation entry when the host only provides the legacy coach callback', () => {
    const onCoach = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => {
      root?.render(
        <OfferCard offer={offer} selected onToggleSelect={vi.fn()} onCoach={onCoach} onView={vi.fn()} />,
      );
    });
    expect(host!.querySelector('[data-action="start-negotiation"]')).not.toBeNull();
    expect(host!.querySelector('[data-action="open-negotiation-coach"]')).toBeNull();
    act(() => host!.querySelector<HTMLButtonElement>('[data-action="start-negotiation"]')?.click());
    expect(onCoach).toHaveBeenCalledWith(offer);
  });

  it('demotes preparation to a regular action when comparison owns the primary action', () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => {
      root?.render(
        <OfferCard
          offer={offer}
          selected={false}
          onToggleSelect={vi.fn()}
          onCoach={vi.fn()}
          onNegotiation={vi.fn()}
          emphasis="secondary"
          onView={vi.fn()}
        />,
      );
    });
    expect(host.querySelector('[data-action="start-negotiation"]')?.className).not.toContain('ant-btn-primary');
  });

  it('returns to the owning application only when an offer is bound', () => {
    const onOpenApplication = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => {
      root?.render(
        <OfferCard
          offer={{ ...offer, application_id: 42 }}
          selected={false}
          onToggleSelect={vi.fn()}
          onCoach={vi.fn()}
          onNegotiation={vi.fn()}
          onView={vi.fn()}
          onOpenApplication={onOpenApplication}
        />,
      );
    });
    act(() => host?.querySelector<HTMLButtonElement>('[data-action="open-application"]')?.click());
    expect(onOpenApplication).toHaveBeenCalledWith(42);
  });

  it('renders a historical unbound offer as read-only', () => {
    const onNegotiation = vi.fn();
    const onCoach = vi.fn();
    const onView = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => {
      root?.render(
        <OfferCard
          offer={{ ...offer, application_id: undefined }}
          selected={false}
          onToggleSelect={vi.fn()}
          onCoach={onCoach}
          onNegotiation={onNegotiation}
          onView={onView}
        />,
      );
    });

    expect(host.querySelector('[data-action="start-negotiation"]')).toBeNull();
    expect(host.querySelector('[data-action="view-offer"]')?.textContent).toContain('查看');
    expect(host.textContent).toContain('历史 Offer 仅支持只读查看');
    act(() => host?.querySelector<HTMLButtonElement>('[data-action="view-offer"]')?.click());
    expect(onView).toHaveBeenCalledWith({ ...offer, application_id: undefined });
    expect(onNegotiation).not.toHaveBeenCalled();
    expect(onCoach).not.toHaveBeenCalled();
  });

  it('disables the return action when the owning application is unavailable', () => {
    const onOpenApplication = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => {
      root?.render(
        <OfferCard
          offer={{ ...offer, application_id: 42 }}
          applicationLinkState="unavailable"
          selected={false}
          onToggleSelect={vi.fn()}
          onCoach={vi.fn()}
          onView={vi.fn()}
          onOpenApplication={onOpenApplication}
        />,
      );
    });

    const back = host.querySelector<HTMLButtonElement>('[data-action="open-application"]');
    expect(back?.disabled).toBe(true);
    expect(host.textContent).toContain('所属投递当前不可见');
    act(() => back?.click());
    expect(onOpenApplication).not.toHaveBeenCalled();
  });
});
