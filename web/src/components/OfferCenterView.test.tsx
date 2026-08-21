// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Offer } from '@/types/offer';
import OfferCenterView from './OfferCenterView';
import { confirmOfferNegotiationProposal, createOfferNegotiationProposal } from '@/services/offers';

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: () => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }),
});

const queryState = vi.hoisted(() => ({ offers: [] as Offer[] }));
vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: queryState.offers, isError: false, isFetching: false, isLoading: false, refetch: vi.fn() }) }));
vi.mock('@/services/offers', () => ({
  listOffers: vi.fn(),
  listOfferComparisonDimensions: vi.fn(async () => []),
  listOfferComparisonValues: vi.fn(async () => []),
  createOfferComparisonDimension: vi.fn(),
  updateOfferComparisonDimension: vi.fn(),
  saveOfferComparisonValue: vi.fn(),
  clearOfferComparisonValue: vi.fn(),
  listOfferNegotiationProposals: vi.fn(async () => []),
  getOfferNegotiationProposal: vi.fn(async () => null),
  createOfferNegotiationProposal: vi.fn(),
  confirmOfferNegotiationProposal: vi.fn(),
  OfferNegotiationError: class OfferNegotiationError extends Error {
    constructor(public status: number, public code: string | null) { super(code ?? 'error'); }
  },
}));
vi.mock('@/components/OfferCard', () => ({ default: ({ offer, onToggleSelect }: { offer: Offer; onToggleSelect: (id: number) => void }) => (
  <button type="button" data-testid={`select-${offer.id}`} onClick={() => onToggleSelect(offer.id)}>{offer.company_name}</button>
) }));
vi.mock('@/components/AddOfferForm', () => ({ default: () => null }));
vi.mock('@/components/OfferCompareDrawer', () => ({ default: ({ offers, onNegotiation }: { offers: Offer[]; onNegotiation?: (offer: Offer) => void }) => (
  <div data-testid="compare-offers">
    {offers.map((offer) => offer.id).join(',')}
    <button type="button" data-testid="compare-negotiate" onClick={() => onNegotiation?.(offers[0])}>prepare</button>
  </div>
) }));

const offer = (id: number): Offer => ({
  id, company_name: `Company ${id}`, position_name: 'Engineer', status: 'pending',
  base_monthly: 30000, months_per_year: 13, signing_bonus: 10000, equity: '', perks: '',
  deadline: '', notes: '', assessment: '', total_cash: 400000,
  created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-01T00:00:00Z',
});

describe('OfferCenterView comparison guardrails', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;
  });

  it('guides entry with one primary action when there are no offers', async () => {
    queryState.offers = [];
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<OfferCenterView applications={[]} onCoach={vi.fn()} />); });
    expect(host.textContent).toContain('录入第一份 Offer');
    expect(host.textContent).not.toContain('比较维度');
  });

  it('shows one offer facts and pending confirmations without comparison controls', async () => {
    queryState.offers = [{ ...offer(1), deadline: '', equity: '', base_monthly: 0 }];
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<OfferCenterView applications={[]} onCoach={vi.fn()} />); });
    expect(host.textContent).toContain('待确认');
    expect(host.textContent).toContain('截止时间');
    expect(host.textContent).not.toContain('对比选中');
  });

  it('shows comparison selection only for two or more offers and dimensions only after entry', async () => {
    queryState.offers = [offer(1), offer(2)];
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<OfferCenterView applications={[]} onCoach={vi.fn()} />); });
    expect(host.textContent).toContain('选择至少两份 Offer 进行比较');
    expect(host.querySelector('[data-selected-comparison-dimensions]')).toBeNull();
  });

  it('does not show unsupported aggregate claims for any offer count', async () => {
    queryState.offers = [offer(1), offer(2)];
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<OfferCenterView applications={[]} onCoach={vi.fn()} />); });

    expect(host.textContent).not.toContain('平均年总包');
    expect(host.textContent).not.toContain('最高签字费');
  });

  it('preserves the user selection order in comparison columns', async () => {
    queryState.offers = [offer(1), offer(2)];
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<OfferCenterView applications={[]} onCoach={vi.fn()} />); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="select-2"]')?.click(); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="select-1"]')?.click(); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('button:not([data-testid])')?.click(); });
    expect(host?.querySelector('[data-testid="compare-offers"]')?.textContent).toContain('2,1');
  });

  it('closes comparison before opening the negotiation drawer', async () => {
    queryState.offers = [offer(1), offer(2)];
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<OfferCenterView applications={[]} onCoach={vi.fn()} />); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="select-1"]')?.click(); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="select-2"]')?.click(); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('button:not([data-testid])')?.click(); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="compare-negotiate"]')?.click(); });
    expect(host?.querySelector('[data-testid="offer-negotiation-drawer"]')).not.toBeNull();
  });

  it('keeps comparison reads and evidence expansion free of negotiation writes', async () => {
    queryState.offers = [offer(1), offer(2)];
    vi.mocked(createOfferNegotiationProposal).mockClear();
    vi.mocked(confirmOfferNegotiationProposal).mockClear();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root?.render(<OfferCenterView applications={[]} onCoach={vi.fn()} />); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="select-1"]')?.click(); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="select-2"]')?.click(); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('button:not([data-testid])')?.click(); });
    expect(host?.querySelector('[data-testid="compare-offers"]')).not.toBeNull();
    expect(createOfferNegotiationProposal).not.toHaveBeenCalled();
    expect(confirmOfferNegotiationProposal).not.toHaveBeenCalled();
  });
});
