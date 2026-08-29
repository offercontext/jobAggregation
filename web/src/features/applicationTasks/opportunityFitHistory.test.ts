import { describe, expect, it } from 'vitest';
import { adaptOpportunityFitHistory } from './opportunityFitHistory';

function v1(id: number, createdAt: string, overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    id,
    application_id: 7,
    resume_id: 3,
    status: 'triage_complete',
    summary: { text: `V1 summary ${id}` },
    created_at: createdAt,
    recommendation: 'hold',
    source_fingerprint_sha256: 'current-source',
    ...overrides,
  };
}

function v2(
  id: number,
  createdAt: string,
  stageStatus: 'generating' | 'provider_unknown' | 'ready' | 'confirmed' | 'source_conflict' = 'ready',
  overrides: Record<string, unknown> = {},
) {
  const proposal = stageStatus === 'ready' || stageStatus === 'confirmed'
    ? {
      schema_version: 2,
      stage: 'triage',
      summary: { text: `V2 summary ${id}` },
    }
    : undefined;
  return {
    id,
    review_id: id,
    application_id: 7,
    schema_version: 2,
    status: 'active',
    created_at: createdAt,
    latest_stage: {
      id: id * 10,
      stage_id: id * 10,
      review_id: id,
      application_id: 7,
      schema_version: 2,
      stage: 'triage',
      stage_status: stageStatus,
      created_at: createdAt,
      source_fingerprint_sha256: 'current-source',
      ...(proposal ? { proposal } : {}),
    },
    ...overrides,
  };
}

function readySources(v1Rows: readonly unknown[] = [], v2Rows: readonly unknown[] = []) {
  return {
    applicationId: 7,
    currentSourceFingerprint: 'current-source',
    v1: { status: 'ready' as const, value: v1Rows },
    v2: { status: 'ready' as const, value: v2Rows },
  };
}

describe('adaptOpportunityFitHistory', () => {
  it('merges in business-time order, then V2 ordinal, then numeric identity', () => {
    const result = adaptOpportunityFitHistory(readySources(
      [v1(5, '2026-08-30T10:00:00Z')],
      [
        v2(4, '2026-08-30T12:00:00+02:00'),
        v2(6, '2026-08-30T10:00:00Z'),
        v2(3, '2026-08-30T09:00:00Z'),
      ],
    ));
    expect(result.items.map((item) => item.internalKey)).toEqual(['v2:7:6', 'v2:7:4', 'v1:7:5', 'v2:7:3']);
    expect(result.items.every((item) => Object.isFrozen(item) && Object.isFrozen(item.details))).toBe(true);
    expect(Object.isFrozen(result.items)).toBe(true);
  });

  it('keeps unknown and source-conflict states without inventing provider data', () => {
    const pendingWithProposal = v2(8, '2026-08-30T09:00:00Z', 'provider_unknown');
    (pendingWithProposal.latest_stage as Record<string, unknown>).proposal = {
      schema_version: 2,
      stage: 'triage',
      summary: { text: 'Unconfirmed provider proposal' },
    };
    const pendingWithMalformedProposal = v2(10, '2026-08-30T07:00:00Z', 'generating');
    (pendingWithMalformedProposal.latest_stage as Record<string, unknown>).proposal = null;
    const conflictWithMalformedProposal = v2(11, '2026-08-30T06:00:00Z', 'source_conflict');
    (conflictWithMalformedProposal.latest_stage as Record<string, unknown>).proposal = { schema_version: 1 };
    const result = adaptOpportunityFitHistory(readySources([], [
      pendingWithProposal,
      v2(9, '2026-08-30T08:00:00Z', 'source_conflict'),
      pendingWithMalformedProposal,
      conflictWithMalformedProposal,
    ]));
    expect(result.items.map((item) => ({
      summary: item.summary,
      resultState: item.details.resultState,
      sourceState: item.sourceState,
    }))).toEqual([
      { summary: '结果待确认', resultState: 'result_unknown', sourceState: 'current' },
      { summary: '资料已更新，结果待确认', resultState: 'source_changed', sourceState: 'source_changed' },
      { summary: '结果待确认', resultState: 'result_unknown', sourceState: 'current' },
      { summary: '资料已更新，结果待确认', resultState: 'source_changed', sourceState: 'source_changed' },
    ]);
  });

  it('reports one unavailable route instead of treating it as an empty history', () => {
    const result = adaptOpportunityFitHistory({
      ...readySources([v1(1, '2026-08-30T09:00:00Z')]),
      v2: { status: 'loading' as const },
    });
    expect(result.items).toHaveLength(1);
    expect(result.partial).toBe(true);
    expect(result.unavailableSources).toEqual(['v2']);
  });

  it('rejects foreign, malformed and non-canonical calendar rows', () => {
    const result = adaptOpportunityFitHistory(readySources([
      v1(1, '2026-02-30T09:00:00Z'),
      v1(2, '2026-08-30T09:00:00Z', { application_id: 99 }),
      v1(3, '2026-08-30T09:00:60Z'),
      v1(4, '2026-08-30T09:00:00Z', { id: 0 }),
    ]));
    expect(result.items).toEqual([]);
    expect(result.invalidRecordCount).toBe(4);
  });

  it('deduplicates equivalent rows and drops every conflicting duplicate independent of order', () => {
    const same = v1(1, '2026-08-30T09:00:00Z');
    const equivalent = v1(1, '2026-08-30T11:00:00+02:00');
    const conflict = v1(1, '2026-08-30T09:00:00Z', { summary: { text: 'different' } });
    const forward = adaptOpportunityFitHistory(readySources([same, equivalent, conflict]));
    const reverse = adaptOpportunityFitHistory(readySources([conflict, equivalent, same]));
    expect(forward.items).toEqual([]);
    expect(reverse.items).toEqual([]);
    expect(forward.invalidRecordCount).toBe(3);
    expect(reverse.invalidRecordCount).toBe(3);

    const deduped = adaptOpportunityFitHistory(readySources([same, equivalent]));
    expect(deduped.items).toHaveLength(1);
    expect(deduped.invalidRecordCount).toBe(0);
    expect(deduped.items[0].createdAt).toBe('2026-08-30T09:00:00.000Z');
  });

  it('enforces V2 root/stage ownership and schema identity', () => {
    const rootMismatch = v2(1, '2026-08-30T09:00:00Z', 'ready', { id: 2 });
    const stageReviewMismatch = v2(2, '2026-08-30T09:00:00Z');
    stageReviewMismatch.latest_stage.review_id = 99;
    const stageAppMismatch = v2(3, '2026-08-30T09:00:00Z');
    stageAppMismatch.latest_stage.application_id = 99;
    const stageSchemaMismatch = v2(4, '2026-08-30T09:00:00Z');
    stageSchemaMismatch.latest_stage.schema_version = 1;
    const result = adaptOpportunityFitHistory(readySources([], [
      rootMismatch,
      stageReviewMismatch,
      stageAppMismatch,
      stageSchemaMismatch,
    ]));
    expect(result.items).toEqual([]);
    expect(result.invalidRecordCount).toBe(4);
  });

  it('survives throwing getters and revoked source proxies', () => {
    const throwingRow = new Proxy({}, { get() { throw new Error('revoked'); } });
    expect(() => adaptOpportunityFitHistory(readySources([throwingRow]))).not.toThrow();
    expect(adaptOpportunityFitHistory(readySources([throwingRow])).invalidRecordCount).toBe(1);

    const throwingIterator = new Proxy([v1(1, '2026-08-30T09:00:00Z')], {
      get(target, property, receiver) {
        if (property === Symbol.iterator) throw new Error('iterator revoked');
        return Reflect.get(target, property, receiver);
      },
    });
    const hostileResult = adaptOpportunityFitHistory({
      applicationId: 7,
      v1: throwingIterator,
      v2: { status: 'ready', value: [] },
    });
    expect(hostileResult.items).toEqual([]);
    expect(hostileResult.partial).toBe(true);
    expect(hostileResult.unavailableSources).toEqual(['v1']);

    const revocable = Proxy.revocable([v1(1, '2026-08-30T09:00:00Z')], {});
    revocable.revoke();
    const result = adaptOpportunityFitHistory({
      applicationId: 7,
      v1: revocable.proxy,
      v2: { status: 'ready', value: [] },
    });
    expect(result.partial).toBe(true);
    expect(result.unavailableSources).toEqual(['v1']);
  });

  it('labels rows when the source fingerprint changed without exposing the fingerprint', () => {
    const result = adaptOpportunityFitHistory({
      ...readySources([v1(1, '2026-08-30T09:00:00Z', { source_fingerprint_sha256: 'old-source' })]),
      currentSourceFingerprint: 'current-source',
    });
    expect(result.items[0].sourceState).toBe('source_changed');
    expect(JSON.stringify(result.items[0])).not.toContain('old-source');
    expect(JSON.stringify(result.items[0])).not.toContain('current-source');
  });
});
