import { describe, expect, it } from 'vitest';
import {
  classifyMaterialRecord,
  projectExperienceMaterials,
  projectExternalReferences,
  type MaterialProjectionInput,
  type MaterialRecord,
} from './materialClassification';

const captureMetadata = {
  origin_note_id: 21,
  application_event_id: 7,
  note_fingerprint: 'fingerprint',
  capture_schema_version: 'interview-note-capture-v1',
};

const confirmedCapture: MaterialRecord = {
  id: 11,
  kind: 'knowledge_note',
  origin_kind: 'confirmed_interview_capture',
  source_id: 31,
  version_id: 41,
  captured_at: '2026-08-29T10:00:00Z',
  capture_metadata: captureMetadata,
  content: { blocks: [{ block_id: 'b1', text: '我完成了迁移。' }] },
};

const externalSource = (id: number, source_kind = 'markdown'): MaterialRecord => ({
  id,
  kind: 'knowledge_source',
  source_kind,
  title: `参考资料 ${id}`,
});

const story: MaterialRecord = {
  id: 51,
  kind: 'interview_story',
  title: '订单延迟排查',
  status: 'active',
  current_version_id: 71,
  story_revision: 2,
  version_number: 1,
  source_states: [],
  version: { confirmed_at: '2026-08-28T10:00:00Z', content: { title: { text: '订单延迟排查' } } },
};

describe('material classification', () => {
  it('prioritizes typed confirmed capture metadata over external source kind', () => {
    expect(classifyMaterialRecord({ ...confirmedCapture, source_kind: 'markdown' })).toEqual({
      kind: 'confirmed_capture',
    });
  });

  it('keeps orphan and broken captures unavailable instead of falling back to references', () => {
    const orphan = { ...confirmedCapture, capture_metadata: undefined, source_kind: 'markdown' };
    expect(classifyMaterialRecord(orphan)).toEqual({
      kind: 'captured_unavailable',
      reason: 'capture_relation_invalid',
    });
    expect(projectExternalReferences({ records: [orphan] }).items).toEqual([]);
    expect(projectExperienceMaterials({ records: [orphan] }).items).toEqual([]);
    expect(classifyMaterialRecord({ id: 12, source_kind: 'markdown', capture_metadata: null })).toEqual({
      kind: 'captured_unavailable',
      reason: 'capture_relation_invalid',
    });
  });

  it('classifies only unannotated external markdown, text, and bundle sources as references', () => {
    const records = [externalSource(1), externalSource(2, 'text'), { ...externalSource(3, 'bundle'), origin_kind: null }];
    expect(records.map((record) => classifyMaterialRecord(record))).toEqual([
      { kind: 'external_reference' },
      { kind: 'external_reference' },
      { kind: 'external_reference' },
    ]);
    expect(projectExternalReferences({ records }).items.map((item) => item.record.id)).toEqual([1, 2, 3]);
  });

  it('requires the confirmed note relation for a captured source row', () => {
    const source = {
      id: 31,
      source_kind: 'captured_interview_note',
      capture_metadata: { ...captureMetadata, source_id: 31 },
      confirmed_note: { id: 21 },
    };
    expect(classifyMaterialRecord(source)).toEqual({ kind: 'confirmed_capture' });
    expect(classifyMaterialRecord({ ...source, confirmed_note: null })).toEqual({
      kind: 'captured_unavailable',
      reason: 'capture_relation_invalid',
    });
  });

  it('does not let a broken capture with the same source identity fall back to references', () => {
    const brokenCapture = {
      id: 11,
      source_id: 31,
      origin_kind: 'confirmed_interview_capture',
    };
    const external = { id: 31, source_kind: 'markdown', title: '同一来源' };
    expect(projectExternalReferences({ records: [external, brokenCapture] }).items).toEqual([]);
    expect(projectExperienceMaterials({ records: [external, brokenCapture] }).state).toBe('unavailable');
  });

  it('does not classify unknown or raw/proposed records into either user list', () => {
    const records: MaterialRecord[] = [
      { id: 1, kind: 'interview_note', title: '原始复盘' },
      { id: 2, kind: 'proposal', source_kind: 'markdown' },
      { id: 3, kind: 'pending', origin_kind: 'confirmed_interview_capture' },
      { id: 4, source_kind: 'pdf', title: '不支持的来源' },
    ];
    expect(projectExperienceMaterials({ records }).items).toEqual([]);
    expect(projectExternalReferences({ records }).items).toEqual([]);
    expect(classifyMaterialRecord(records[3])).toEqual({
      kind: 'unclassified',
      reason: 'unsupported_source_kind',
    });
    expect(projectExternalReferences({ records }).state).toBe('unavailable');
    expect(projectExternalReferences({ records }).hasUnavailable).toBe(true);
  });

  it('does not infer a Story from an untyped object shape', () => {
    expect(classifyMaterialRecord({
      id: 5,
      story_revision: 2,
      current_version_id: 9,
      status: 'active',
    })).toEqual({ kind: 'unclassified', reason: 'missing_source_kind' });
  });

  it('projects stories and confirmed captures exclusively with stable labels', () => {
    const result = projectExperienceMaterials({ records: [story, confirmedCapture, externalSource(9)] });
    expect(result.items.map((item) => item.kind)).toEqual(['experience_story', 'confirmed_capture']);
    expect(result.items.map((item) => item.label)).toEqual(['经历故事', '已确认面试片段']);
    expect(result.items.some((item) => item.record.id === 9)).toBe(false);
    expect(result.state).toBe('ready');
  });

  it('keeps loading, error, empty, and partial source states distinct', () => {
    const states: Array<[MaterialProjectionInput, string]> = [
      [{ recordsState: 'loading' }, 'loading'],
      [{ recordsState: 'error' }, 'error'],
      [{ recordsState: 'empty', records: [] }, 'empty'],
      [{ recordsState: 'partial', records: [confirmedCapture] }, 'partial'],
    ];
    for (const [input, state] of states) {
      expect(projectExperienceMaterials(input).state).toBe(state);
    }
    expect(projectExperienceMaterials({ records: { status: 'loading', value: null } }).state).toBe('loading');
    expect(projectExperienceMaterials({ records: { status: 'absent', value: null } }).state).toBe('error');
  });

  it('does not turn malformed source values, holes, or envelopes into an empty success', () => {
    const valid = externalSource(91);
    const malformedInputs = [
      { records: null },
      { records: 42 },
      { records: { status: 'ready', value: 'not-an-array' } },
    ] as unknown as MaterialProjectionInput[];
    for (const input of malformedInputs) {
      const result = projectExternalReferences(input);
      expect(result.items).toEqual([]);
      expect(result.state).toBe('unavailable');
      expect(result.hasUnavailable).toBe(true);
    }
    const invalidStatus = projectExternalReferences({
      records: { status: 'not-a-source-state', value: [valid] } as never,
    });
    expect(invalidStatus.items.map((item) => item.record.id)).toEqual([91]);
    expect(invalidStatus.state).toBe('partial');
    expect(invalidStatus.hasUnavailable).toBe(true);

    const withHole = [valid, , externalSource(92)] as unknown as MaterialRecord[];
    const partial = projectExternalReferences({ records: withHole });
    expect(partial.items.map((item) => item.record.id)).toEqual([91, 92]);
    expect(partial.state).toBe('partial');
    expect(partial.hasUnavailable).toBe(true);

    const explicitNullCapture = projectExperienceMaterials({
      captures: null,
      confirmedCaptures: [confirmedCapture],
    });
    expect(explicitNullCapture.items).toEqual([]);
    expect(explicitNullCapture.state).toBe('unavailable');
  });

  it('requires a complete schema for explicit Story records', () => {
    const incomplete = { id: 53, kind: 'interview_story', title: '未完成故事', status: 'active' };
    expect(classifyMaterialRecord(incomplete)).toEqual({
      kind: 'unclassified',
      reason: 'not_material_record',
    });
    expect(projectExperienceMaterials({ records: [incomplete] }).state).toBe('unavailable');
    expect(classifyMaterialRecord({
      ...story,
      source_states: [{ state: 'current' }],
    })).toEqual({ kind: 'unclassified', reason: 'not_material_record' });
  });

  it('fails closed for missing identities, unknown capture versions, and invalid event ids', () => {
    expect(projectExternalReferences({ records: [{ source_kind: 'markdown', title: '没有 ID' }] }).state).toBe('unavailable');
    expect(classifyMaterialRecord({
      ...confirmedCapture,
      capture_metadata: { ...captureMetadata, capture_schema_version: 'interview-note-capture-v2' },
    })).toEqual({ kind: 'captured_unavailable', reason: 'capture_relation_invalid' });
    expect(classifyMaterialRecord({
      ...confirmedCapture,
      capture_metadata: { ...captureMetadata, application_event_id: 0 },
    })).toEqual({ kind: 'captured_unavailable', reason: 'capture_relation_invalid' });
    expect(classifyMaterialRecord({
      ...confirmedCapture,
      capture_metadata: { ...captureMetadata, application_event_id: null },
    })).toEqual({ kind: 'confirmed_capture' });
    expect(classifyMaterialRecord({
      ...confirmedCapture,
      capture_metadata: { ...captureMetadata, application_event_id: null },
      application_event: null,
    })).toEqual({ kind: 'confirmed_capture' });
    expect(classifyMaterialRecord({
      ...confirmedCapture,
      capture_metadata: { ...captureMetadata, application_event_id: null },
      application_event: { id: 7 },
    })).toEqual({ kind: 'captured_unavailable', reason: 'capture_relation_invalid' });
    expect(classifyMaterialRecord({
      ...externalSource(5),
      kind: 'unexpected',
    })).toEqual({ kind: 'unclassified', reason: 'unsupported_source_kind' });
  });

  it('does not fall back to a row id when source_id is explicitly invalid', () => {
    const invalidSourceId = { ...externalSource(131), source_id: undefined };
    expect(projectExternalReferences({ records: [invalidSourceId] }).items).toEqual([]);
    expect(projectExternalReferences({ records: [invalidSourceId] }).state).toBe('unavailable');
    expect(classifyMaterialRecord({
      ...confirmedCapture,
      source_id: undefined,
    })).toEqual({ kind: 'captured_unavailable', reason: 'capture_relation_invalid' });
  });

  it('chooses the same canonical duplicate regardless of input order', () => {
    const first = { ...externalSource(101), title: 'Zeta copy', summary: 'z' };
    const second = { ...externalSource(101), title: 'Alpha copy', summary: 'a' };
    const forward = projectExternalReferences({ records: [first, second] });
    const reverse = projectExternalReferences({ records: [second, first] });
    expect(forward.items).toEqual(reverse.items);
    expect(forward.items[0]?.title).toBe('Alpha copy');

    const current = { ...externalSource(102), title: '当前副本' };
    const unavailable = { ...externalSource(102), title: '不可用副本', source_states: [{ state: 'error' }] };
    const safety = projectExternalReferences({ records: [current, unavailable] });
    expect(safety.items[0]?.sourceState).toBe('unavailable');
    expect(safety.state).toBe('unavailable');
  });

  it('marks error, deleted, and unknown source states unavailable', () => {
    for (const state of ['error', 'deleted', 'unknown']) {
      const result = projectExternalReferences({ records: [{ ...externalSource(110), source_states: [{ state }] }] });
      expect(result.items[0]?.sourceState).toBe('unavailable');
      expect(result.state).toBe('unavailable');
      expect(result.hasUnavailable).toBe(true);
      expect(result.unavailable).toContainEqual({ kind: 'source_unavailable', reason: 'source_state_unavailable' });
    }
  });

  it('survives hostile getters and does not expose a mutable source object', () => {
    const hostile = new Proxy({ id: 120, source_kind: 'markdown' }, {
      get() { throw new Error('hostile getter'); },
      has() { throw new Error('hostile has'); },
      ownKeys() { throw new Error('hostile keys'); },
      getOwnPropertyDescriptor() { throw new Error('hostile descriptor'); },
    });
    expect(() => projectExternalReferences({ records: [hostile] })).not.toThrow();
    expect(projectExternalReferences({ records: [hostile] }).state).toBe('unavailable');

    const source = externalSource(121);
    const result = projectExternalReferences({ records: [source] });
    expect(result.items[0]?.record).not.toBe(source);
    expect(Object.isFrozen(result.items[0]?.record)).toBe(true);

    const nested = projectExternalReferences({
      records: [{
        ...externalSource(122),
        source_states: [{
          state: 'current',
          source_kind: 'interview_note',
          source_stable_id: 'note:1',
          source_version_or_snapshot: 'snapshot:1',
        }],
      }],
    });
    const frozenStates = nested.items[0]?.record.source_states as unknown[];
    expect(Object.isFrozen(frozenStates)).toBe(true);
    expect(Object.isFrozen(frozenStates[0])).toBe(true);
    expect(() => frozenStates.push({})).toThrow();
  });

  it('survives revoked and non-iterable array proxies at every source boundary', () => {
    const revokedValue = Proxy.revocable([externalSource(132)], {});
    revokedValue.revoke();
    expect(() => projectExternalReferences({ records: { status: 'ready', value: revokedValue.proxy } as never })).not.toThrow();
    expect(projectExternalReferences({ records: { status: 'ready', value: revokedValue.proxy } as never }).state).toBe('unavailable');

    const nonIterable = new Proxy([externalSource(133)], {
      get(target, property, receiver) {
        if (property === Symbol.iterator) throw new Error('hostile iterator');
        return Reflect.get(target, property, receiver);
      },
    });
    const result = projectExternalReferences({ records: nonIterable as never });
    expect(result.items.map((item) => item.record.id)).toEqual([133]);

    const revokedStates = Proxy.revocable([{ state: 'current' }], {});
    revokedStates.revoke();
    expect(() => projectExternalReferences({ records: [{ ...externalSource(134), source_states: revokedStates.proxy }] })).not.toThrow();
    expect(projectExternalReferences({ records: [{ ...externalSource(134), source_states: revokedStates.proxy }] }).state).toBe('unavailable');

    const hostileStoryStates = Proxy.revocable([], {});
    hostileStoryStates.revoke();
    expect(() => classifyMaterialRecord({
      ...story,
      id: 135,
      source_states: hostileStoryStates.proxy,
    })).not.toThrow();
  });

  it('fails closed for contradictory source statuses and non-empty empty values', () => {
    const conflict = projectExternalReferences({
      records: { status: 'error', value: [externalSource(136)] },
      recordsState: 'ready',
    } as never);
    expect(conflict.items).toEqual([]);
    expect(conflict.state).toBe('unavailable');

    const emptyWithValue = projectExternalReferences({
      records: { status: 'empty', value: [externalSource(137)] },
    } as never);
    expect(emptyWithValue.items).toEqual([]);
    expect(emptyWithValue.state).toBe('unavailable');

    const outerEmptyWithValue = projectExternalReferences({
      records: [externalSource(138)],
      recordsState: 'empty',
    });
    expect(outerEmptyWithValue.items).toEqual([]);
    expect(outerEmptyWithValue.state).toBe('unavailable');

    const ownUndefined = projectExternalReferences({ records: undefined, recordsState: 'empty' });
    expect(ownUndefined.items).toEqual([]);
    expect(ownUndefined.state).toBe('unavailable');
  });

  it('fails closed for a hostile projector input and for ready-without-value', () => {
    const hostileInput = new Proxy({}, {
      ownKeys() { throw new Error('hostile input'); },
    }) as MaterialProjectionInput;
    expect(() => projectExternalReferences(hostileInput)).not.toThrow();
    expect(projectExternalReferences(hostileInput).state).toBe('unavailable');
    const revoked = Proxy.revocable({}, {});
    revoked.revoke();
    expect(() => projectExternalReferences(revoked.proxy as MaterialProjectionInput)).not.toThrow();
    expect(projectExternalReferences(revoked.proxy as MaterialProjectionInput).state).toBe('unavailable');
    expect(() => classifyMaterialRecord(revoked.proxy)).not.toThrow();
    expect(classifyMaterialRecord(revoked.proxy)).toEqual({ kind: 'unclassified', reason: 'not_material_record' });
    expect(projectExternalReferences({ recordsState: 'ready' }).state).toBe('unavailable');
  });

  it('freezes projections so route changes cannot mutate shared source rows', () => {
    const result = projectExternalReferences({ records: [externalSource(1)] });
    expect(Object.isFrozen(result)).toBe(true);
    expect(Object.isFrozen(result.items)).toBe(true);
    expect(Object.isFrozen(result.items[0])).toBe(true);
  });
});
