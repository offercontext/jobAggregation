// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { App as AntApp } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import type { Application } from '@/types/application';
import type { Offer } from '@/types/offer';
import AddOfferForm from './AddOfferForm';
import { createOffer, updateOffer } from '@/services/offers';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const getComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element) => getComputedStyle(element);

vi.mock('@/services/offers', () => ({
  createOffer: vi.fn(),
  updateOffer: vi.fn(),
}));

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: () => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }),
});
if (!HTMLElement.prototype.scrollIntoView) HTMLElement.prototype.scrollIntoView = vi.fn();

const applications = [
  { id: 7, company_name: '星云数据', position_name: '后端工程师' },
  { id: 8, company_name: '远山科技', position_name: '平台工程师' },
] as Application[];

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

function field(name: string) {
  const element = document.body.querySelector<HTMLInputElement>(`#${name}`);
  if (!element) throw new Error(`Missing field: ${name}`);
  return element;
}

function setField(name: string, value: string) {
  const input = field(name);
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

async function selectApplication(optionText: string) {
  const select = field('application_id').closest<HTMLElement>('.ant-select');
  const trigger = select?.querySelector<HTMLElement>('.ant-select-selector') ?? select;
  if (!trigger) throw new Error('Missing application select');
  await act(async () => {
    trigger.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    trigger.click();
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"], .ant-select-item-option')]
    .find((item) => item.textContent?.includes(optionText));
  if (!option) throw new Error(`Missing application option: ${optionText}`);
  await act(async () => {
    option.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    option.click();
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

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

it('prefills a new Offer from its application while preserving manually edited fields', async () => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

  await act(async () => {
    root?.render(
      <QueryClientProvider client={client}>
        <AntApp>
          <AddOfferForm open onClose={vi.fn()} applications={applications} />
        </AntApp>
      </QueryClientProvider>,
    );
  });

  await selectApplication('星云数据');
  expect(field('company_name').value).toBe('星云数据');
  expect(field('position_name').value).toBe('后端工程师');

  setField('company_name', '候选人手动填写的公司');
  await selectApplication('远山科技');
  expect(field('company_name').value).toBe('候选人手动填写的公司');
  expect(field('position_name').value).toBe('平台工程师');

  setField('company_name', '');
  await selectApplication('星云数据');
  expect(field('company_name').value).toBe('星云数据');
  expect(field('position_name').value).toBe('后端工程师');
}, 15_000);
