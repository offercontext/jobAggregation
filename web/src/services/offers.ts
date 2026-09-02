import type {
  Offer,
  OfferComparisonDimension,
  OfferComparisonRead,
  OfferComparisonValue,
  OfferInput,
  OfferNegotiationBrief,
  OfferNegotiationPending,
  OfferNegotiationProposal,
  OfferNegotiationRead,
  OfferNegotiationPreview,
} from '@/types/offer';
import { OfferNegotiationError } from '@/types/offer';
import { createApiClient } from './http';

const http = createApiClient({ baseURL: '/api', timeout: 10000 });
const aiHttp = createApiClient({ baseURL: '/api', timeout: 130000 });

export { OfferNegotiationError } from '@/types/offer';

export interface OfferNegotiationInput {
  idempotency_key: string;
  dimension_ids: number[];
  goal: string;
  concerns: string;
  scenario: string;
  source_fingerprint?: string;
}

export type OfferNegotiationPreviewInput = Omit<OfferNegotiationInput, 'idempotency_key' | 'source_fingerprint'>;

type OfferNegotiationResponse = OfferNegotiationProposal | OfferNegotiationPending;

function isOfferNegotiationError(error: unknown): error is { response?: { status?: number; data?: { error_code?: unknown } } } {
  return typeof error === 'object' && error !== null && 'response' in error;
}

async function offerNegotiationRequest<T>(request: () => Promise<{ data: T }>): Promise<T> {
  try {
    return (await request()).data;
  } catch (error) {
    if (isOfferNegotiationError(error)) {
      const response = error.response;
      const code = typeof response?.data?.error_code === 'string' ? response.data.error_code : null;
      throw new OfferNegotiationError(response?.status ?? 0, code);
    }
    throw new OfferNegotiationError(0, null);
  }
}

export function createOfferNegotiationProposal(
  offerId: number,
  input: OfferNegotiationInput,
  entrypoint: 'ui' | 'pilot' = 'ui',
): Promise<OfferNegotiationResponse> {
  return offerNegotiationRequest(() => aiHttp.post<OfferNegotiationResponse>(
    `/offers/${offerId}/negotiation/proposals`, input, { headers: { 'X-OfferPilot-Entrypoint': entrypoint } },
  ));
}

export function previewOfferNegotiation(
  offerId: number,
  input: OfferNegotiationPreviewInput,
): Promise<OfferNegotiationPreview> {
  return offerNegotiationRequest(() => http.post<OfferNegotiationPreview>(
    `/offers/${offerId}/negotiation/preview`, input,
  ));
}

export function listOfferNegotiationProposals(offerId: number): Promise<OfferNegotiationProposal[]> {
  return offerNegotiationRequest(() => http.get<OfferNegotiationProposal[]>(
    `/offers/${offerId}/negotiation/proposals`,
  ));
}

export function getOfferNegotiationProposal(proposalId: number): Promise<OfferNegotiationRead> {
  return offerNegotiationRequest(() => http.get<OfferNegotiationRead>(
    `/offer-negotiation/proposals/${proposalId}`,
  ));
}

export function confirmOfferNegotiationProposal(
  proposalId: number,
  input: { confirmation_key: string; selected_blocks: string[]; edited_content: Record<string, string> },
  entrypoint: 'ui' | 'pilot' = 'ui',
): Promise<OfferNegotiationBrief> {
  return offerNegotiationRequest(() => http.post<OfferNegotiationBrief>(
    `/offer-negotiation/proposals/${proposalId}/confirm`, input,
    { headers: { 'X-OfferPilot-Entrypoint': entrypoint } },
  ));
}

export async function listOffers(status?: string): Promise<Offer[]> {
  const { data } = await http.get<Offer[]>('/offers', { params: status ? { status } : {} });
  return data ?? [];
}

export async function getOffer(id: number): Promise<Offer> {
  const { data } = await http.get<Offer>(`/offers/${id}`);
  return data;
}

export async function createOffer(input: OfferInput): Promise<Offer> {
  const { data } = await http.post<Offer>('/offers', input);
  return data;
}

export async function updateOffer(id: number, input: OfferInput): Promise<Offer> {
  const { data } = await http.put<Offer>(`/offers/${id}`, input);
  return data;
}

export async function deleteOffer(id: number): Promise<void> {
  await http.delete(`/offers/${id}`);
}

export async function compareOffers(ids: number[]): Promise<Offer[]> {
  const { data } = await http.get<Offer[]>('/offers/compare', { params: { ids: ids.join(',') } });
  return data ?? [];
}

export async function listOfferComparisonDimensions(
  includeArchived = false,
): Promise<OfferComparisonDimension[]> {
  const { data } = await http.get<OfferComparisonDimension[]>('/offers/comparison-dimensions', {
    params: includeArchived ? { include_archived: true } : {},
  });
  return data ?? [];
}

export async function createOfferComparisonDimension(label: string): Promise<OfferComparisonDimension> {
  const { data } = await http.post<OfferComparisonDimension>('/offers/comparison-dimensions', { label });
  return data;
}

export async function updateOfferComparisonDimension(
  id: number,
  input: { label?: string; archived?: boolean },
): Promise<OfferComparisonDimension> {
  const { data } = await http.patch<OfferComparisonDimension>(
    `/offers/comparison-dimensions/${id}`,
    input,
  );
  return data;
}

export async function listOfferComparisonValues(offerId: number): Promise<OfferComparisonValue[]> {
  const { data } = await http.get<OfferComparisonValue[]>(
    `/offers/${offerId}/comparison-values`,
  );
  return data ?? [];
}

export async function saveOfferComparisonValue(
  offerId: number,
  dimensionId: number,
  valueText: string,
): Promise<OfferComparisonValue> {
  const { data } = await http.put<OfferComparisonValue>(
    `/offers/${offerId}/comparison-values/${dimensionId}`,
    { value_text: valueText },
  );
  return data;
}

export async function clearOfferComparisonValue(
  offerId: number,
  dimensionId: number,
): Promise<void> {
  await http.delete(`/offers/${offerId}/comparison-values/${dimensionId}`);
}

export async function readOfferComparison(
  offerIds: number[],
  dimensionIds: number[] = [],
): Promise<OfferComparisonRead> {
  const { data } = await http.get<OfferComparisonRead>('/offers/comparison', {
    params: {
      ids: offerIds.join(','),
      ...(dimensionIds.length > 0 ? { dimension_ids: dimensionIds.join(',') } : {}),
    },
  });
  return data;
}
