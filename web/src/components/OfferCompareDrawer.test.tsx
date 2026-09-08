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

async function mountComparison(offers: Offer[], onEdit = vi.fn()) {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root?.render(<OfferCompareDrawer open offers={offers} onClose={vi.fn()} onEdit={onEdit} />); });
}

it('hides only equal known facts and retains missing and uncertain rows', async () => {
  const offers = [offer(1), offer(2)].map((item) => ({ ...item, perks: '待确认' }));
  readComparison.mockResolvedValue({ offers, dimensions: [], missing: [] });
  await mountComparison(offers);
  await act(async () => { host?.querySelector<HTMLButtonElement>('[role="switch"]')?.click(); });
  expect(host?.querySelector('[data-field="annual"]')).toBeNull();
  expect(host?.querySelector('[data-field="signing"]')).toBeNull();
  expect(host?.querySelector('[data-field="equity"]')).not.toBeNull();
  expect(host?.querySelector('[data-field="perks"]')).not.toBeNull();
  expect(host?.textContent).toContain('已隐藏 4 项');
});

it('preserves selected order even if the server returns another order and edits the exact offer', async () => {
  const offers = [offer(2), offer(1)];
  readComparison.mockResolvedValue({ offers: [...offers].reverse(), dimensions: [], missing: [] });
  const onEdit = vi.fn();
  await mountComparison(offers, onEdit);
  expect([...host!.querySelectorAll('article')].map((item) => item.getAttribute('data-testid'))).toEqual(['offer-comparison-header-2', 'offer-comparison-header-1']);
  await act(async () => { host?.querySelector<HTMLButtonElement>('[data-action="edit-offer"][data-offer-id="2"]')?.click(); });
  expect(onEdit).toHaveBeenCalledWith(offers[0]);
});

it('shows a recoverable read error then retries without dropping current facts', async () => {
  const offers = [offer(1), offer(2)];
  readComparison.mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ offers, dimensions: [], missing: [] });
  await mountComparison(offers);
  expect(host?.textContent).toContain('对比详情暂时无法更新');
  expect(host?.textContent).toContain('Company 1');
  const retryButton = [...host!.querySelectorAll('button')].find((button) => button.textContent?.replace(/\s/g, '') === '重试');
  expect(retryButton).toBeTruthy();
  await act(async () => { retryButton?.click(); });
  expect(readComparison).toHaveBeenCalledTimes(2);
  expect(host?.textContent).not.toContain('对比详情暂时无法更新');
});

it('ignores late snapshots after an Offer edit and does not refetch on equivalent arrays', async () => {
  const offers = [offer(1), offer(2)];
  let resolveOld!: (value: OfferComparisonRead) => void;
  readComparison.mockImplementationOnce(() => new Promise<OfferComparisonRead>((resolve) => { resolveOld = resolve; }));
  await mountComparison(offers);
  const edited = [{ ...offers[0], company_name: 'Updated Company', base_monthly: 40000 }, offers[1]];
  readComparison.mockResolvedValue({ offers: edited, dimensions: [], missing: [] });
  await act(async () => { root?.render(<OfferCompareDrawer open offers={edited} onClose={vi.fn()} />); });
  await act(async () => { resolveOld({ offers, dimensions: [], missing: [] }); });
  expect(host?.textContent).toContain('Updated Company');
  expect(host?.textContent).not.toContain('Company 1');
  await act(async () => { root?.render(<OfferCompareDrawer open offers={[...edited]} onClose={vi.fn()} />); });
  expect(readComparison).toHaveBeenCalledTimes(2);
});

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
  expect(host.textContent).toContain('未补充');
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
  expect(host.querySelector('[data-missing="true"]')?.textContent).toContain('未补充');
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
