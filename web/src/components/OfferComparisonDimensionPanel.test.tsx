// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Offer, OfferComparisonDimension, OfferComparisonValue } from '@/types/offer';
import OfferComparisonDimensionPanel from './OfferComparisonDimensionPanel';
import { listOfferComparisonValues } from '@/services/offers';

const serviceState = vi.hoisted(() => ({
  dimensions: [] as OfferComparisonDimension[],
  values: [] as OfferComparisonValue[],
  create: vi.fn(),
  update: vi.fn(),
  save: vi.fn(),
  clear: vi.fn(),
}));

vi.mock('@/services/offers', () => ({
  listOfferComparisonDimensions: vi.fn(async () => serviceState.dimensions),
  createOfferComparisonDimension: serviceState.create,
  updateOfferComparisonDimension: serviceState.update,
  listOfferComparisonValues: vi.fn(async () => serviceState.values),
  saveOfferComparisonValue: serviceState.save,
  clearOfferComparisonValue: serviceState.clear,
}));

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

const dimension = (id: number, label = `维度 ${id}`): OfferComparisonDimension => ({
  id,
  label,
  archived_at: null,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
});

let root: Root | null = null;
let host: HTMLDivElement | null = null;

function render(onSelectionChange?: (dimensionIds: number[]) => void, onChanged?: () => void) {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root?.render(<OfferComparisonDimensionPanel offers={[offer(1), offer(2)]} onSelectionChange={onSelectionChange} onChanged={onChanged} />));
  return host;
}

describe('OfferComparisonDimensionPanel', () => {
  beforeEach(() => {
    serviceState.dimensions = [dimension(1, '通勤')];
    serviceState.values = [];
    serviceState.create.mockReset();
    serviceState.update.mockReset();
    serviceState.save.mockReset();
    serviceState.clear.mockReset();
    vi.mocked(listOfferComparisonValues).mockReset().mockImplementation(async () => serviceState.values);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;
  });

  it('creates dimensions, edits values, clears values, and archives dimensions explicitly', async () => {
    serviceState.create.mockResolvedValue(dimension(2, '成长空间'));
    serviceState.save.mockResolvedValue({
      id: 1,
      offer_id: 1,
      dimension_id: 1,
      value_text: '地铁 35 分钟',
      created_at: '2026-07-01T00:00:00Z',
      updated_at: '2026-07-01T00:00:00Z',
    });
    serviceState.clear.mockResolvedValue(undefined);
    serviceState.update.mockResolvedValue({ ...dimension(1, '通勤'), archived_at: '2026-07-02T00:00:00Z' });
    const onSelectionChange = vi.fn();
    const onChanged = vi.fn();
    const rendered = render(onSelectionChange, onChanged);

    await act(async () => {});
    const labelInput = rendered.querySelector<HTMLInputElement>('input[placeholder="新比较维度"]');
    expect(labelInput).not.toBeNull();
    await act(async () => {
      if (labelInput) {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(labelInput, '成长空间');
        labelInput.dispatchEvent(new Event('change', { bubbles: true }));
      }
      rendered.querySelector<HTMLButtonElement>('button[data-action="create-dimension"]')?.click();
    });
    expect(serviceState.create).toHaveBeenCalledWith('成长空间');

    const valueInput = rendered.querySelector<HTMLInputElement>('input[data-offer-id="1"][data-dimension-id="1"]');
    expect(valueInput).not.toBeNull();
    await act(async () => {
      if (valueInput) {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(valueInput, '地铁 35 分钟');
        valueInput.dispatchEvent(new Event('change', { bubbles: true }));
      }
      rendered.querySelector<HTMLButtonElement>('button[data-action="save-value"][data-offer-id="1"]')?.click();
    });
    expect(serviceState.save).toHaveBeenCalledWith(1, 1, '地铁 35 分钟');
    expect(onChanged).toHaveBeenCalledTimes(1);

    await act(async () => {
      rendered.querySelector<HTMLButtonElement>('button[data-action="clear-value"][data-offer-id="1"]')?.click();
    });
    expect(serviceState.clear).toHaveBeenCalledWith(1, 1);
    expect(onChanged).toHaveBeenCalledTimes(2);

    await act(async () => {
      rendered.querySelector<HTMLButtonElement>('button[data-action="archive-dimension"][data-dimension-id="1"]')?.click();
    });
    expect(serviceState.update).toHaveBeenCalledWith(1, { archived: true });
    expect(onSelectionChange).toHaveBeenLastCalledWith([]);
  });

  it('limits active selection to eight dimensions and reports selected ids', async () => {
    serviceState.dimensions = Array.from({ length: 9 }, (_, index) => dimension(index + 1));
    const onSelectionChange = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(
      <OfferComparisonDimensionPanel offers={[offer(1), offer(2)]} onSelectionChange={onSelectionChange} />,
    ));
    await act(async () => {});

    const checkboxes = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    expect(checkboxes).toHaveLength(9);
    for (const checkbox of checkboxes.slice(0, 8)) {
      await act(async () => checkbox.click());
    }
    expect(checkboxes[8].disabled).toBe(true);
    expect(onSelectionChange).toHaveBeenLastCalledWith([1, 2, 3, 4, 5, 6, 7, 8]);
    expect(host.textContent).toContain('最多选择 8 个比较维度');
  });

  it('renders each active dimension as a structured settings group', async () => {
    serviceState.values = [
      {
        id: 1,
        offer_id: 1,
        dimension_id: 1,
        value_text: '地铁 35 分钟',
        created_at: '2026-07-01T00:00:00Z',
        updated_at: '2026-07-01T00:00:00Z',
      },
    ];
    const rendered = render();
    await act(async () => {});

    const card = rendered.querySelector('[data-testid="comparison-dimension-card"]');
    expect(card).not.toBeNull();
    expect(card?.textContent).toContain('通勤');
    expect(card?.textContent).toContain('Company 1');
    expect(card?.textContent).toContain('Company 2');
    expect(rendered.querySelector('[data-action="create-dimension"]')?.textContent).toContain('新增维度');
    expect(rendered.querySelectorAll('[data-action="save-value"]')).toHaveLength(2);
    expect(rendered.querySelectorAll('[data-action="clear-value"]')).toHaveLength(2);
    expect(rendered.textContent).toContain('已选择 0/8');
  });

  it('keeps historical unbound values read-only in active dimensions', async () => {
    host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host);
    await act(async () => { root?.render(<OfferComparisonDimensionPanel offers={[{ ...offer(1), application_id: undefined }, offer(2)]} />); });
    expect(host.querySelector('[data-action="save-value"][data-offer-id="1"]')).toBeNull();
    expect(host.querySelector('[data-action="clear-value"][data-offer-id="1"]')).toBeNull();
    expect(host.querySelector('input[data-offer-id="1"]')).toBeNull();
    expect(host.textContent).toContain('历史未绑定，仅可查看');
    expect(host.querySelector('[data-action="save-value"][data-offer-id="2"]')).not.toBeNull();
  });

  it('initializes from parent selection and preserves it when another dimension is selected', async () => {
    serviceState.dimensions = [dimension(1), dimension(2)];
    const onSelectionChange = vi.fn();
    host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host);
    await act(async () => { root?.render(<OfferComparisonDimensionPanel offers={[offer(1), offer(2)]} selectedDimensionIds={[1]} onSelectionChange={onSelectionChange} />); });
    const checkboxes = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    expect(checkboxes[0].checked).toBe(true);
    expect(host.textContent).toContain('已选择 1/8');
    await act(async () => { checkboxes[1].click(); });
    expect(onSelectionChange).toHaveBeenLastCalledWith([1, 2]);
  });

  it('ignores a late previous Offer-set read', async () => {
    let resolveOld!: (values: OfferComparisonValue[]) => void;
    vi.mocked(listOfferComparisonValues).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    const rendered = render();
    await act(async () => {});
    serviceState.values = [{ id: 1, offer_id: 2, dimension_id: 1, value_text: '新值', created_at: '', updated_at: '' }];
    await act(async () => { root?.render(<OfferComparisonDimensionPanel offers={[offer(2), offer(3)]} />); });
    expect(rendered.querySelector<HTMLInputElement>('input[data-offer-id="2"]')?.value).toBe('新值');
    await act(async () => { resolveOld([{ ...serviceState.values[0], value_text: '旧值' }]); });
    expect(rendered.querySelector<HTMLInputElement>('input[data-offer-id="2"]')?.value).toBe('新值');
    expect(rendered.textContent).not.toContain('Company 1');
  });

  it('keeps archived values readable and marks blank values as missing', async () => {
    serviceState.dimensions = [
      { ...dimension(1, '通勤'), archived_at: '2026-07-02T00:00:00Z' },
    ];
    serviceState.values = [{
      id: 1,
      offer_id: 1,
      dimension_id: 1,
      value_text: '地铁 35 分钟',
      created_at: '2026-07-01T00:00:00Z',
      updated_at: '2026-07-01T00:00:00Z',
    }];
    const rendered = render();
    await act(async () => {});
    expect(rendered.querySelector('[data-testid="archived-dimension-value"]')?.textContent).toContain('地铁 35 分钟');
    expect(rendered.textContent).not.toContain('可用于比较');
    expect(rendered.textContent).toContain('已归档，仅历史可读');
  });
});
