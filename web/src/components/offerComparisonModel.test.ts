import { describe, expect, it } from 'vitest';
import type { Offer } from '@/types/offer';
import { annualSalaryEstimate, comparisonInsights, deadlineDay, isSameKnownRow, textFact } from './offerComparisonModel';

const offer = (patch: Partial<Offer> = {}): Offer => ({
  id: 1, application_id: 1, company_name: '远帆科技', position_name: '后端工程师', status: 'pending',
  base_monthly: 25000, months_per_year: 16, signing_bonus: 10000, total_cash: 410000,
  equity: '', perks: '', deadline: '2026-09-20', notes: '', assessment: '', created_at: '', updated_at: '', ...patch,
});

describe('Offer comparison facts', () => {
  it('compares annual salary without mixing the one-off signing bonus into it', () => {
    const offers = [offer(), offer({ id: 2, base_monthly: 26000, months_per_year: 14, signing_bonus: 90000, total_cash: 454000, deadline: '2026-09-18' })];
    expect(annualSalaryEstimate(offers[0])).toBe(400000);
    expect(comparisonInsights(offers)).toMatchObject({ salaryGap: 36000, higherSalaryId: 1, deadlineGap: 2, earliestDeadlineIds: [2] });
    expect(offers.map((item) => item.id)).toEqual([1, 2]);
  });

  it('keeps zero values, absence and explicit uncertainty distinct', () => {
    expect(textFact('0 元').state).toBe('known');
    expect(textFact('无').state).toBe('known');
    expect(textFact('  ').state).toBe('missing');
    expect(textFact('五险一金，具体缴纳基数待确认').state).toBe('uncertain');
  });

  it('only hides known equal values, never missing or uncertain pairs', () => {
    expect(isSameKnownRow([textFact('无'), textFact('无')])).toBe(true);
    expect(isSameKnownRow([textFact(''), textFact('')])).toBe(false);
    expect(isSameKnownRow([textFact('待确认'), textFact('待确认')])).toBe(false);
    expect(isSameKnownRow([textFact('0 元'), textFact('')])).toBe(false);
    expect(isSameKnownRow([textFact('相同')])).toBe(false);
  });

  it('counts missing fields separately from recorded uncertainty', () => {
    expect(comparisonInsights([offer(), offer({ id: 2, perks: '五险一金，缴纳基数待确认' })]))
      .toMatchObject({ missingCount: 3, uncertainCount: 1, missingLabels: ['期权', '福利'] });
  });

  it.each(['2026-02-30', '2026-13-01', '9月18日', '', '2026-09-18T00:00:00Z'])('does not invent day differences from %s', (value) => {
    expect(deadlineDay(value)).toBeNull();
    expect(comparisonInsights([offer(), offer({ id: 2, deadline: value })]).deadlineGap).toBeNull();
  });

  it('uses calendar days across month/year boundaries and leap days', () => {
    expect(deadlineDay('2028-03-01')! - deadlineDay('2028-02-28')!).toBe(2);
    expect(deadlineDay('2027-01-01')! - deadlineDay('2026-12-31')!).toBe(1);
  });

  it('does not award a higher side to tied or incomplete salary data', () => {
    expect(comparisonInsights([offer(), offer({ id: 2 })]).higherSalaryId).toBeNull();
    expect(comparisonInsights([offer(), offer({ id: 2, months_per_year: 0 })]).salaryGap).toBeNull();
    expect(annualSalaryEstimate(offer({ base_monthly: Number.NaN }))).toBeNull();
  });

  it('reports a range for three offers without assigning a pairwise winner', () => {
    const result = comparisonInsights([offer(), offer({ id: 2, base_monthly: 20000, deadline: '2026-09-18' }), offer({ id: 3, base_monthly: 30000, deadline: '2026-09-18' })]);
    expect(result).toMatchObject({ salaryGap: 160000, higherSalaryId: null, deadlineGap: 2, earliestDeadlineIds: [2, 3] });
  });
});
