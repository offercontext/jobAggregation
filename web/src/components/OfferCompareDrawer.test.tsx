// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, expect, it, vi } from 'vitest';
import type { Offer, OfferComparisonRead } from '@/types/offer';
import OfferCompareDrawer from './OfferCompareDrawer';

const readComparison = vi.hoisted(() => vi.fn());
vi.mock('@/services/offers', () => ({ readOfferComparison: readComparison }));

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: () => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }),
});

const offer = (id: number): Offer => ({
  id,
  application_id: id,
  company_name: `Company ${id}`,
  position_name: 'Engineer',
  status: 'pending',
  base_monthly: 30000,
  months_per_year: 13,
  signing_bonus: 10000,
  equity: '',
  perks: '',
  deadline: '',
  notes: '',
  assessment: '',
  total_cash: 400000,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
});

let root: Root | null = null;
let host: HTMLDivElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  readComparison.mockReset();
});

it('renders only factual comparison rows and preserves explicit Offer action IDs', async () => {
  const offers = [offer(2), offer(1)];
  const comparison: OfferComparisonRead = {
    offers,
    dimensions: [{
      id: 1,
      label: '通勤',
      values: [
        { offer_id: 1, value_text: '地铁 35 分钟' },
        { offer_id: 2, value_text: null },
      ],
    }],
    missing: [{ offer_id: 2, path: 'offer_snapshot/dimensions/1/value_text', label: '通勤' }],
  };
  readComparison.mockResolvedValue(comparison);
  const onNegotiation = vi.fn();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root?.render(
      <OfferCompareDrawer
        open
        onClose={vi.fn()}
        offers={offers}
        dimensionIds={[1]}
        onNegotiation={onNegotiation}
      />,
    );
  });

  expect(readComparison).toHaveBeenCalledWith([2, 1], [1]);
  expect(host.textContent).toContain('通勤');
  expect(host.textContent).toContain('尚未填写');
  for (const forbidden of ['评分', '排名', '权重', '最佳 Offer', '推荐接受', '推荐拒绝']) {
    expect(host.textContent).not.toContain(forbidden);
  }
  await act(async () => {
    host?.querySelector<HTMLButtonElement>('button[data-action="start-negotiation"][data-offer-id="2"]')?.click();
  });
  expect(onNegotiation).toHaveBeenCalledWith(offers[0]);
});

it('keeps historical unbound offers read-only while allowing bound preparation', async () => {
  const offers = [{ ...offer(2), application_id: undefined }, { ...offer(1), application_id: 7 }];
  readComparison.mockResolvedValue({ offers, dimensions: [], missing: [] } satisfies OfferComparisonRead);
  const onNegotiation = vi.fn();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);

  await act(async () => {
    root?.render(
      <OfferCompareDrawer
        open
        onClose={vi.fn()}
        offers={offers}
        dimensionIds={[]}
        onNegotiation={onNegotiation}
      />,
    );
  });

  expect(host.querySelector('[data-action="start-negotiation"][data-offer-id="2"]')).toBeNull();
  const boundAction = host.querySelector<HTMLButtonElement>('[data-action="start-negotiation"][data-offer-id="1"]');
  expect(boundAction).not.toBeNull();
  await act(async () => boundAction?.click());
  expect(onNegotiation).toHaveBeenCalledWith(offers[1]);
});

it('renders a zero signing bonus as a factual comparison value', async () => {
  const offers = [{ ...offer(2), signing_bonus: 0 }, offer(1)];
  readComparison.mockResolvedValue({ offers, dimensions: [], missing: [] } satisfies OfferComparisonRead);
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);

  await act(async () => {
    root?.render(<OfferCompareDrawer open onClose={vi.fn()} offers={offers} dimensionIds={[]} />);
  });

  const fixedFacts = host.querySelector('[data-section="fixed-facts"]');
  expect(fixedFacts?.textContent).toContain('0');
  expect(fixedFacts?.textContent).not.toContain('灏氭湭濉啓');
});

it('renders structured factual groups and rich Offer headers', async () => {
  const offers = [offer(2), offer(1)];
  readComparison.mockResolvedValue({
    offers,
    dimensions: [{ id: 1, label: '通勤', values: [{ offer_id: 1, value_text: '地铁 35 分钟' }, { offer_id: 2, value_text: null }] }],
    missing: [{ offer_id: 2, path: 'offer_snapshot/dimensions/1/value_text', label: '通勤' }],
  } satisfies OfferComparisonRead);
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root?.render(<OfferCompareDrawer open onClose={vi.fn()} offers={offers} dimensionIds={[1]} />);
  });

  expect(host.querySelector('[data-testid="offer-comparison-header-2"]')?.textContent)
    .toContain('Company 2');
  expect(host.querySelector('[data-testid="offer-comparison-header-2"]')?.textContent)
    .toContain('Engineer');
  expect(host.querySelector('[data-section="fixed-facts"]')).not.toBeNull();
  expect(host.querySelector('[data-section="custom-dimensions"]')).not.toBeNull();
  expect(host.querySelector('[data-missing="true"]')?.textContent).toContain('尚未填写');
});

it('keeps custom dimension rows out of the fixed salary facts table', async () => {
  const offers = [offer(2), offer(1)];
  readComparison.mockResolvedValue({
    offers,
    dimensions: [{ id: 1, label: '通勤', values: [{ offer_id: 1, value_text: '地铁 35 分钟' }, { offer_id: 2, value_text: null }] }],
    missing: [],
  } satisfies OfferComparisonRead);
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root?.render(<OfferCompareDrawer open onClose={vi.fn()} offers={offers} dimensionIds={[1]} />); });

  expect(host.querySelector('[data-section="fixed-facts"]')?.textContent).not.toContain('通勤');
  expect(host.querySelector('[data-section="custom-dimensions"]')?.textContent).toContain('通勤');
  expect(host.querySelector('[data-section="custom-dimensions"] table')).not.toBeNull();
});
