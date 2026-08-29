export type OfferWorkspaceMode = 'entry' | 'single' | 'selection' | 'comparison';

export type OfferBindingState = 'bound' | 'unbound';

/**
 * Offer records created before application binding was required still exist in
 * the API. Keep them visible, but make the missing canonical owner explicit in
 * the workspace instead of silently inventing one on the client.
 */
export function listOfferBindingState(offer: { application_id?: number | null }): OfferBindingState {
  return Number.isInteger(offer.application_id) && Number(offer.application_id) > 0 ? 'bound' : 'unbound';
}

export function getOfferWorkspaceMode(count: number, comparisonOpen: boolean): OfferWorkspaceMode {
  if (count <= 0) return 'entry';
  if (count === 1) return 'single';
  return comparisonOpen ? 'comparison' : 'selection';
}

export function listMissingOfferFacts(offer: {
  base_monthly?: number | null;
  months_per_year?: number | null;
  deadline?: string | null;
  equity?: string | null;
}): string[] {
  const missing: string[] = [];
  if (!offer.base_monthly) missing.push('月薪');
  if (!offer.months_per_year) missing.push('年薪月数');
  if (!offer.deadline?.trim()) missing.push('截止时间');
  if (!offer.equity?.trim()) missing.push('股权或期权');
  return missing;
}
