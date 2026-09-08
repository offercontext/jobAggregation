import type { Offer } from '@/types/offer';

export interface ComparisonFact {
  value: string | number | null;
  text: string;
  state: 'known' | 'missing' | 'uncertain';
}

export function textFact(value: string | null | undefined): ComparisonFact {
  const text = value?.trim() ?? '';
  if (!text) return { value: null, text: '未补充', state: 'missing' };
  return { value: text, text, state: /待确认|待核实|未确定|待沟通/.test(text) ? 'uncertain' : 'known' };
}

export function numberFact(value: number | null | undefined, format: (value: number) => string): ComparisonFact {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? { value, text: format(value), state: 'known' }
    : textFact(null);
}

export function annualSalaryEstimate(offer: Offer): number | null {
  if (!Number.isFinite(offer.base_monthly) || offer.base_monthly < 0 || !Number.isFinite(offer.months_per_year) || offer.months_per_year <= 0) return null;
  const value = offer.base_monthly * offer.months_per_year;
  return Number.isFinite(value) ? value : null;
}

/** Date-only Offer deadlines are calendar dates, not local-midnight instants. */
export function deadlineDay(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(0, 0, 0, 0);
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  return date.getTime() / 86400000;
}

export function isSameKnownRow(values: ComparisonFact[]): boolean {
  return values.length >= 2 && values.every((value) => value.state === 'known' && value.value === values[0].value);
}

export function formatWan(value: number): string {
  return new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 1, maximumFractionDigits: 2 }).format(value / 10000);
}

export function formatCash(value: number): string {
  return value === 0 ? '0 元' : value < 10000 ? `${value.toLocaleString('zh-CN')} 元` : `${formatWan(value)} 万元`;
}

export function comparisonInsights(offers: Offer[]) {
  const salaries = offers.map(annualSalaryEstimate);
  const salaryComplete = offers.length >= 2 && salaries.every((salary) => salary !== null);
  const knownSalaries = salaries.filter((salary): salary is number => salary !== null);
  const salaryGap = salaryComplete ? Math.max(...knownSalaries) - Math.min(...knownSalaries) : null;
  const higherSalaryId = offers.length === 2 && salaryGap !== null && salaryGap > 0
    ? offers[salaries[0]! > salaries[1]! ? 0 : 1].id : null;
  const days = offers.map((offer) => deadlineDay(offer.deadline ?? ''));
  const deadlineComplete = offers.length >= 2 && days.every((day) => day !== null);
  const knownDays = days.filter((day): day is number => day !== null);
  const deadlineGap = deadlineComplete ? Math.max(...knownDays) - Math.min(...knownDays) : null;
  const earliestDeadlineIds = deadlineGap !== null && deadlineGap > 0
    ? offers.filter((_, index) => days[index] === Math.min(...knownDays)).map((offer) => offer.id) : [];
  const gaps: { label: string; state: ComparisonFact['state'] }[] = [];
  offers.forEach((offer) => {
    const facts: [string, ComparisonFact][] = [
      ['月薪', numberFact(offer.base_monthly, String)],
      ['计薪月数', offer.months_per_year > 0 ? numberFact(offer.months_per_year, String) : textFact(null)],
      ['签字费', numberFact(offer.signing_bonus, String)],
      ['期权', textFact(offer.equity)], ['福利', textFact(offer.perks)], ['回复截止', textFact(offer.deadline)],
    ];
    facts.forEach(([label, value]) => { if (value.state !== 'known') gaps.push({ label, state: value.state }); });
  });
  return {
    salaryGap, higherSalaryId, deadlineGap, earliestDeadlineIds,
    missingCount: gaps.filter((gap) => gap.state === 'missing').length,
    uncertainCount: gaps.filter((gap) => gap.state === 'uncertain').length,
    missingLabels: [...new Set(gaps.filter((gap) => gap.state === 'missing').map((gap) => gap.label))],
  };
}
