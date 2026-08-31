import { beforeEach, describe, expect, it, vi } from 'vitest';

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock('@/services/http', () => ({ createApiClient: () => http }));

const service = await import('./service');

beforeEach(() => {
  http.get.mockReset();
  http.post.mockReset();
});

describe('review-readiness service contracts', () => {
  it('freezes candidate, proposal, decision and owner recovery URLs and exact bodies', async () => {
    http.get.mockResolvedValueOnce({ data: {
      schema_version: 1,
      state: 'ready',
      note_id: 7,
      proposal_id: 11,
      candidates: [{
        application_id: 3,
        event_id: 5,
        note_id: 7,
        proposal_id: 11,
        proposal_schema_version: 2,
        focus_id: 'focus-1',
        statement: '先给结论，再说明取舍。',
        source_note_revision: 4,
        source_note_fingerprint: `sha256:${'1'.repeat(64)}`,
        source_proposal_hash: `sha256:${'2'.repeat(64)}`,
        candidate_fingerprint: `sha256:${'3'.repeat(64)}`,
        evidence: [{ ordinal: 0, source_path: '/difficulty_points', excerpt: '取舍不清楚', excerpt_sha256: `sha256:${'4'.repeat(64)}`, source_field_sha256: `sha256:${'5'.repeat(64)}` }],
      }],
    } });
    const candidates = await service.getReviewReadinessCandidates(7, 11);
    expect(http.get).toHaveBeenCalledWith('/interview-notes/7/readiness-feedback-candidates', { params: { proposal_id: 11 } });

    const request = {
      proposal_id: 11,
      focus_id: candidates.candidates[0]!.focus_id,
      expected_note_revision: 4,
      expected_candidate_fingerprint: `sha256:${'3'.repeat(64)}`,
      idempotency_key: '00000000-0000-4000-8000-000000000001',
      user_note: '',
    } as const;
    http.post.mockResolvedValueOnce({ data: {
      schema_version: 1,
      operation_id: '00000000-0000-4000-8000-000000000002',
      action_call_id: '00000000-0000-4000-8000-000000000003',
      action_name: 'save_review_readiness_signal',
      status: 'proposed',
      created: true,
      replayed: false,
      confirmation_token: 'a'.repeat(64),
    } });
    await service.proposeReviewReadinessAction(7, request);
    expect(http.post).toHaveBeenNthCalledWith(1, '/interview-notes/7/readiness-focus-actions', request);

    http.post.mockResolvedValueOnce({ data: {
      schema_version: 1,
      operation_id: '00000000-0000-4000-8000-000000000002',
      action_name: 'save_review_readiness_signal',
      status: 'committed',
      result: { schema_version: 1, action_name: 'save_review_readiness_signal', outcome: 'created', signal_id: 9, signal_version_id: 10, signal_revision: 1, source_status: 'current' },
      replayed: false,
      direct_commit: true,
    } });
    await service.decideProductAction('00000000-0000-4000-8000-000000000002', {
      confirmation_token: 'a'.repeat(64),
      decision: 'modify',
      edited_payload: { user_note: '下次先给结论。' },
    });
    expect(http.post).toHaveBeenNthCalledWith(2, '/product-actions/00000000-0000-4000-8000-000000000002/decisions', {
      confirmation_token: 'a'.repeat(64),
      decision: 'modify',
      edited_payload: { user_note: '下次先给结论。' },
    });

    http.get.mockResolvedValueOnce({ data: {
      schema_version: 1,
      operation_id: '00000000-0000-4000-8000-000000000002',
      action_call_id: '00000000-0000-4000-8000-000000000003',
      action_name: 'save_review_readiness_signal',
      status: 'proposed',
      confirmation_token: 'a'.repeat(64),
      allowed_decisions: ['approve', 'modify', 'reject'],
      rejection_only: false,
      live_source_state: 'current',
    } });
    await service.recoverSignalOwnerAction(7, '00000000-0000-4000-8000-000000000002');
    expect(http.get).toHaveBeenLastCalledWith('/interview-notes/7/readiness-focus-actions/00000000-0000-4000-8000-000000000002');
  });

  it('uses exact practice/advisory and owner-scoped Undo routes', async () => {
    http.get.mockResolvedValueOnce({ data: {
      schema_version: 1,
      application_id: 3,
      event_id: 20,
      items: [],
    } });
    await service.getEventReadinessFeedback(3, 20);
    expect(http.get).toHaveBeenNthCalledWith(1, '/applications/3/events/20/readiness-feedback');

    http.get.mockResolvedValueOnce({ data: {
      schema_version: 1,
      signalId: 8,
      versionId: 9,
      targetEventId: 20,
      practiceSourceFingerprint: `sha256:${'a'.repeat(64)}`,
      practiceTargetFingerprint: `sha256:${'b'.repeat(64)}`,
      state: 'available',
      practiceState: 'not_started',
      selected: false,
      title: '先给结论',
      sourceLabel: '第 2 轮面试复盘',
    } });
    await service.getReadinessPracticeFocus(9, 20);
    expect(http.get).toHaveBeenNthCalledWith(2, '/interview-practice/focus/9', { params: { target_event_id: 20 } });

    const undo = { schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000004', compensation_kind: 'undo:save_review_readiness_signal', status: 'failed', result: { reason: 'dependent_practice_exists' }, replayed: true };
    http.post.mockResolvedValueOnce({ data: undo }).mockResolvedValueOnce({ data: { ...undo, compensation_kind: 'undo:confirm_interview_story', status: 'committed', replayed: false, result: { kind: 'interview_story_undo_applied_v1', story_id: 6, current_version_id: 7, story_revision: 2, status: 'archived' } } });
    const failedSignalUndo = await service.undoReadinessSignal(3, 8, '00000000-0000-4000-8000-000000000002');
    const committedStoryUndo = await service.undoInterviewStory(6, '00000000-0000-4000-8000-000000000002');
    expect(failedSignalUndo).toMatchObject({ status: 'failed', replayed: true });
    expect(committedStoryUndo).toMatchObject({ status: 'committed', replayed: false });
    expect(http.post).toHaveBeenNthCalledWith(1, '/applications/3/readiness-signals/8/undo', { parent_operation_id: '00000000-0000-4000-8000-000000000002' });
    expect(http.post).toHaveBeenNthCalledWith(2, '/interview-stories/6/product-action-undo', { parent_operation_id: '00000000-0000-4000-8000-000000000002' });
  });

  it('fails closed when Signal or Story Undo receives the other action compensation kind', async () => {
    const base = {
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000004', status: 'committed', result: {}, replayed: false,
    };
    http.post
      .mockResolvedValueOnce({ data: { ...base, compensation_kind: 'undo:confirm_interview_story' } })
      .mockResolvedValueOnce({ data: { ...base, compensation_kind: 'undo:save_review_readiness_signal' } });
    await expect(service.undoReadinessSignal(3, 8, '00000000-0000-4000-8000-000000000002')).rejects.toThrow();
    await expect(service.undoInterviewStory(6, '00000000-0000-4000-8000-000000000002')).rejects.toThrow();
  });

  it('fails closed when a response adds an unapproved field', async () => {
    http.get.mockResolvedValue({ data: {
      schema_version: 1,
      application_id: 3,
      event_id: 20,
      items: [],
      leaked: 'must-not-pass',
    } });
    await expect(service.getEventReadinessFeedback(3, 20)).rejects.toMatchObject({ code: 'review_readiness_invalid_response' });
  });
});
