// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { App as AntApp } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import type { Offer } from '@/types/offer';
import AddOfferForm from './AddOfferForm';
import { createOffer, updateOffer } from '@/services/offers';

vi.mock('@/services/offers', () => ({
  createOffer: vi.fn(),
  updateOffer: vi.fn(),
}));

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: () => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }),
});

const historicalOffer: Offer = {
  id: 9,
  company_name: '历史公司',
  position_name: '工程师',
  status: 'pending',
  base_monthly: 30000,
  months_per_year: 13,
  signing_bonus: 0,
  equity: '',
  perks: '',
  deadline: '',
  notes: '',
  assessment: '',
  total_cash: 390000,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

let root: Root | null = null;
let host: HTMLDivElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.querySelectorAll('.ant-modal-root').forEach((node) => node.remove());
  vi.mocked(createOffer).mockReset();
  vi.mocked(updateOffer).mockReset();
  root = null;
  host = null;
});

it('opens a historical unbound Offer in a non-submittable read-only viewer', async () => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

  await act(async () => {
    root?.render(
      <QueryClientProvider client={client}>
        <AntApp>
          <AddOfferForm open onClose={vi.fn()} applications={[]} editing={historicalOffer} />
        </AntApp>
      </QueryClientProvider>,
    );
  });

  const viewer = document.body.querySelector('[data-offer-mode="read-only"]');
  expect(viewer).not.toBeNull();
  expect(document.body.textContent).toContain('历史 Offer 仅支持只读查看');
  expect(document.body.querySelector('.ant-modal-footer .ant-btn-primary')).toBeNull();
  expect([...document.body.querySelectorAll<HTMLInputElement>('input, textarea')].every((field) => field.disabled)).toBe(true);
  expect(createOffer).not.toHaveBeenCalled();
  expect(updateOffer).not.toHaveBeenCalled();
});
