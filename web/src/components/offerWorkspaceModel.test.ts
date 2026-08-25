import { describe, expect, it } from 'vitest';
import { getOfferWorkspaceMode, listMissingOfferFacts, listOfferBindingState } from './offerWorkspaceModel';

describe('offer progressive disclosure', () => {
  it('shows entry, single facts and comparison in sequence', () => {
    expect(getOfferWorkspaceMode(0, false)).toBe('entry');
    expect(getOfferWorkspaceMode(1, false)).toBe('single');
    expect(getOfferWorkspaceMode(2, false)).toBe('selection');
    expect(getOfferWorkspaceMode(2, true)).toBe('comparison');
  });

  it('labels missing facts instead of converting them to zero', () => {
    expect(listMissingOfferFacts({ base_monthly: 0, months_per_year: 0, deadline: '', equity: '' })).toEqual([
      '月薪', '年薪月数', '截止时间', '股权或期权',
    ]);
  });

  it('identifies offers without an owning application for an explicit warning', () => {
    expect(listOfferBindingState({ application_id: undefined })).toEqual('unbound');
    expect(listOfferBindingState({ application_id: 42 })).toEqual('bound');
  });
});
