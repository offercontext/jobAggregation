import { describe, expect, it } from 'vitest';
import type { ChatMessage, PilotPageContext } from '@/types/chat';
import {
  createPilotPageContextRemovalState,
  deriveActivePageContext,
  pageContextKey,
  pilotPageContextRemovalReducer,
} from '@/lib/pilotPageContext';
import {
  buildChatRequestContext,
  buildTurns,
  collectEvidence,
  evidenceSetIdentity,
  formatEvidenceMeta,
  parseTurnPresentation,
  hydrateMissingPendingAction,
  pendingActionForConversation,
  pendingComposerDisabledReason,
  pendingAutoSelectReducer,
  remainingEvidence,
  shouldApplyConversationRequest,
  isCurrentVisibleConversationRequest,
  reloadConversationTurns,
  toolMeta,
  confirmationInputForRetry,
  confirmationErrorAllowsImmediateRetry,
  confirmationErrorRequiresSync,
  hasConfirmationSettled,
  shouldAbortActiveRequestOnClose,
  shouldAbortActiveRequestOnReplacement,
  clearOwnedConfirmationLock,
  shouldConsumeConfirmationSettlement,
  shouldRestoreConfirmationRetryFocus,
  selectEvidence,
  toolStepSetIdentity,
} from './model';

describe('evidence selection', () => {
  const item = (id: string, title: string, source = 'tool', kind: 'application' = 'application') => ({
    id,
    title,
    source,
    kind: kind as 'application',
  });

  it('dedupes only matching source, id, and display metadata while preserving conflicts and occurrences', () => {
    const evidence = collectEvidence([
      {
        role: 'assistant',
        content: '',
        steps: [
          {
            name: 'list_applications',
            evidence: [
              item('42', 'Same company', 'latest'),
              item('42', 'Same company', 'other-source'),
              item('43', 'Same company', 'latest'),
              item('42', 'Same company', 'latest'),
              item('42', 'Conflicting company', 'latest'),
            ],
          },
        ],
      },
    ]);

    expect(evidence).toHaveLength(4);
    expect(evidence).toContainEqual(
      expect.objectContaining({ source: 'latest', id: '42', title: 'Same company', occurrences: 2 }),
    );
    expect(evidence).toContainEqual(
      expect.objectContaining({ source: 'latest', id: '42', title: 'Conflicting company', occurrences: 1 }),
    );
  });

  it('selects one representative per normalized cluster before filling remaining capacity', () => {
    const selection = selectEvidence(
      [
        item('1', 'Acme #1'),
        item('2', 'Acme #2'),
        item('3', 'Beta'),
        item('4', 'Acme #3'),
        item('5', 'Gamma'),
      ],
      4,
    );

    expect(selection.visible.map((entry) => entry.id)).toEqual(['1', '3', '5', '2']);
    expect(selection.similar.map((entry) => entry.id)).toEqual(['4']);
    expect(selection.remainingCount).toBe(1);
  });

  it('keeps exact records with matching titles distinct and reports all omitted records', () => {
    const selection = selectEvidence(
      [item('1', 'Same title'), item('2', 'Same title'), item('3', 'Another title')],
      1,
    );

    expect(selection.visible.map((entry) => entry.id)).toEqual(['1']);
    expect(selection.similar.map((entry) => entry.id)).toEqual(['2']);
    expect(selection.remainingCount).toBe(2);
  });

  it('formats embedded valid timestamps without changing invalid metadata', () => {
    expect(formatEvidenceMeta('scheduled 2026-07-10T09:05:59+08:00')).toBe('scheduled 2026-07-10 09:05');
    expect(formatEvidenceMeta('scheduled 2026-02-30T09:05:00Z')).toBe('scheduled 2026-02-30T09:05:00Z');
    expect(formatEvidenceMeta('scheduled 2026-07-10T09:05:00Z+08:00')).toBe(
      'scheduled 2026-07-10T09:05:00Z+08:00',
    );
    expect(formatEvidenceMeta('scheduled 2026-07-10T09:05:00Z.')).toBe('scheduled 2026-07-10 17:05.');
    expect(formatEvidenceMeta('scheduled 2026-07-10T09:05.123')).toBe('scheduled 2026-07-10T09:05.123');
    expect(formatEvidenceMeta('scheduled 2026-07-10T09:05:00.abc')).toBe(
      'scheduled 2026-07-10T09:05:00.abc',
    );
    expect(formatEvidenceMeta('scheduled someday')).toBe('scheduled someday');
    expect(formatEvidenceMeta()).toBeUndefined();
  });

  it('changes the evidence-set identity when a conversation supplies different visible or similar records', () => {
    const visible = [item('1', 'Acme', 'applications')];
    const similar = [item('2', 'Acme', 'applications')];

    expect(evidenceSetIdentity(visible, similar)).not.toBe(evidenceSetIdentity(visible, []));
    expect(evidenceSetIdentity(visible, similar)).not.toBe(
      evidenceSetIdentity([item('3', 'Acme', 'applications')], similar),
    );
    expect(evidenceSetIdentity(visible, similar, [item('3', 'Beta', 'applications')])).not.toBe(
      evidenceSetIdentity(visible, similar, [item('4', 'Gamma', 'applications')]),
    );
  });

  it('collects newest evidence first and applies the diversified cap after exact dedupe', () => {
    const evidence = collectEvidence(
      [
        {
          role: 'assistant',
          content: '',
          steps: [{ name: 'list_applications', evidence: [item('old', 'Older', 'applications')] }],
        },
        {
          role: 'assistant',
          content: '',
          steps: [
            {
              name: 'list_applications',
              evidence: [
                item('new-1', 'Acme #1', 'applications'),
                item('new-2', 'Acme #2', 'applications'),
                item('new-3', 'Beta', 'applications'),
              ],
            },
          ],
        },
      ],
      2,
    );

    expect(evidence.map((entry) => entry.id)).toEqual(['new-1', 'new-3']);
  });

  it('retains omitted distinct records for expansion even when they are not similar', () => {
    const items = Array.from({ length: 9 }, (_value, index) => item(String(index + 1), `Record ${index + 1}`));
    const selection = selectEvidence(items, 8);

    expect(selection.similar).toEqual([]);
    expect(selection.remainingCount).toBe(1);
    expect(remainingEvidence(items, selection.visible).map((entry) => entry.id)).toEqual(['9']);
  });

  it('changes the timeline step-set identity for a new tool call or evidence record', () => {
    const current = [{ name: 'get_application', toolCallId: 'call-1', evidence: [item('1', 'Acme', 'applications')] }];

    expect(toolStepSetIdentity(current)).toBe(toolStepSetIdentity([...current]));
    expect(toolStepSetIdentity(current)).not.toBe(
      toolStepSetIdentity([{ ...current[0], toolCallId: 'call-2' }]),
    );
    expect(toolStepSetIdentity(current)).not.toBe(
      toolStepSetIdentity([{ ...current[0], evidence: [item('2', 'Acme', 'applications')] }]),
    );
  });

  it('changes the timeline step-set identity for ID-less rendered tool changes', () => {
    const base = {
      name: 'search_knowledge',
      detail: 'first query',
      resultText: 'first result',
      evidenceUnavailable: true,
      evidence: [
        {
          id: '1',
          source: 'knowledge',
          kind: 'note' as const,
          title: 'first title',
          meta: 'first meta',
          snippet: 'first snippet',
        },
      ],
    };
    const identity = toolStepSetIdentity([base]);

    expect(identity).not.toBe(toolStepSetIdentity([{ ...base, name: 'list_applications' }]));
    expect(identity).not.toBe(toolStepSetIdentity([{ ...base, detail: 'second query' }]));
    expect(identity).not.toBe(toolStepSetIdentity([{ ...base, resultText: 'second result' }]));
    expect(identity).not.toBe(toolStepSetIdentity([{ ...base, evidenceUnavailable: false }]));
    expect(identity).not.toBe(
      toolStepSetIdentity([{ ...base, evidence: [{ ...base.evidence[0], title: 'second title' }] }]),
    );
    expect(identity).not.toBe(
      toolStepSetIdentity([{ ...base, evidence: [{ ...base.evidence[0], meta: 'second meta' }] }]),
    );
    expect(identity).not.toBe(
      toolStepSetIdentity([{ ...base, evidence: [{ ...base.evidence[0], snippet: 'second snippet' }] }]),
    );
    expect(identity).not.toBe(
      toolStepSetIdentity([{ ...base, evidence: [{ ...base.evidence[0], kind: 'knowledge' as const }] }]),
    );
  });
});

describe('confirmation retry focus lifecycle', () => {
  it('restores focus only after a requested retry finishes with an error', () => {
    expect(shouldRestoreConfirmationRetryFocus(true, '网络失败', false)).toBe(true);
    expect(shouldRestoreConfirmationRetryFocus(true, '网络失败', true)).toBe(false);
    expect(shouldRestoreConfirmationRetryFocus(true, null, false)).toBe(false);
    expect(shouldRestoreConfirmationRetryFocus(false, '网络失败', false)).toBe(false);
  });
});

describe('pending auto-selection suppression', () => {
  it('suppresses explicit new chats and restores eligibility after selecting a conversation', () => {
    expect(pendingAutoSelectReducer(false, 'suppress')).toBe(true);
    expect(pendingAutoSelectReducer(true, 'allow')).toBe(false);
  });

  it('rejects a late selection response after an explicit new chat invalidates its generation', () => {
    expect(shouldApplyConversationRequest(3, 3, false)).toBe(true);
    expect(shouldApplyConversationRequest(2, 3, false)).toBe(false);
    expect(shouldApplyConversationRequest(3, 3, true)).toBe(false);
  });
});

describe('visible conversation request isolation', () => {
  it('ignores deferred chat deltas, pending state, and completion after switching from A to B', () => {
    let generation = 0;
    let visible = { conversationId: 1, turns: ['A'], pending: 'A pending' as string | null };
    const requestA = ++generation;
    const apply = (requestGeneration: number, mutation: () => void) => {
      if (isCurrentVisibleConversationRequest(requestGeneration, generation)) mutation();
    };

    generation += 1;
    visible = { conversationId: 2, turns: ['B'], pending: null };
    apply(requestA, () => visible.turns.push('late A delta'));
    apply(requestA, () => { visible.pending = 'late A pending'; });
    apply(requestA, () => { visible = { conversationId: 1, turns: ['A complete'], pending: null }; });

    expect(visible).toEqual({ conversationId: 2, turns: ['B'], pending: null });
  });

  it('ignores deferred confirmation mutations after switching conversations', () => {
    let generation = 4;
    let visible = { conversationId: 2, turns: ['B'], pending: null as string | null, undo: null as string | null };
    const confirmationA = generation;

    generation += 1;
    if (isCurrentVisibleConversationRequest(confirmationA, generation)) {
      visible = { conversationId: 1, turns: ['confirmed A'], pending: 'next A', undo: 'undo A' };
    }

    expect(visible).toEqual({ conversationId: 2, turns: ['B'], pending: null, undo: null });
  });
});

describe('background confirmation settlement', () => {
  const original = {
    tool_name: 'create_application',
    human: 'create',
    confirmation_token: 'original-token',
  };

  it('keeps polling while the original pending action is still durable', () => {
    for (let poll = 0; poll < 240; poll += 1) {
      expect(hasConfirmationSettled(original, 'original-token')).toBe(false);
    }
    expect(hasConfirmationSettled(undefined, 'original-token')).toBe(false);
  });

  it('settles when the pending action clears or is replaced', () => {
    expect(hasConfirmationSettled(null, 'original-token')).toBe(true);
    expect(
      hasConfirmationSettled(
        { ...original, confirmation_token: 'replacement-token' },
        'original-token',
      ),
    ).toBe(true);
  });

});

describe('active request ownership', () => {
  it('preserves confirmation A across new-chat, B selection, and panel close', () => {
    const confirmationA = {
      kind: 'confirmation' as const,
      conversationId: 1,
      confirmationToken: 'token-a',
    };
    let visibleConversationId: number | undefined = 1;

    visibleConversationId = undefined;
    expect(visibleConversationId).toBeUndefined();
    expect(shouldAbortActiveRequestOnClose(confirmationA)).toBe(false);

    visibleConversationId = 2;
    expect(visibleConversationId).toBe(2);
    expect(shouldAbortActiveRequestOnClose(confirmationA)).toBe(false);
  });

  it('keeps an ordinary active chat running when the panel closes', () => {
    expect(
      shouldAbortActiveRequestOnClose({ kind: 'chat', conversationId: 2 }),
    ).toBe(false);
    expect(shouldAbortActiveRequestOnClose(null)).toBe(false);
  });

  it('aborts an ordinary active chat only when a new conversation replaces it', () => {
    expect(shouldAbortActiveRequestOnReplacement({ kind: 'chat', conversationId: 2 })).toBe(true);
    expect(
      shouldAbortActiveRequestOnReplacement({
        kind: 'confirmation',
        conversationId: 1,
        confirmationToken: 'token-a',
      }),
    ).toBe(false);
    expect(shouldAbortActiveRequestOnReplacement(null)).toBe(false);
  });

  it('does not let stale A completion clear a newer lock for A', () => {
    const stale = { confirmationToken: 'old-token' };
    const replacement = { confirmationToken: 'replacement-token' };
    const locks = new Map([[1, stale]]);
    locks.set(1, replacement);

    expect(clearOwnedConfirmationLock(locks, 1, stale)).toBe(false);
    expect(locks.get(1)).toBe(replacement);
    expect(clearOwnedConfirmationLock(locks, 1, replacement)).toBe(true);
    expect(locks.has(1)).toBe(false);
  });

  it('consumes hidden completion only when reopen reconciliation hydrates it', () => {
    const owner = { confirmationToken: 'token-a' };
    const locks = new Map([[1, owner]]);
    let state = {
      phase: 'saving',
      turns: ['partial'],
      pending: 'token-a' as string | null,
      undo: null as string | null,
    };
    let dataChangedCalls = 0;

    if (shouldConsumeConfirmationSettlement(null, 'token-a', false)) {
      clearOwnedConfirmationLock(locks, 1, owner);
    }
    expect(locks.get(1)).toBe(owner);

    if (shouldConsumeConfirmationSettlement(null, 'token-a', true)) {
      const consumed = clearOwnedConfirmationLock(locks, 1, owner);
      if (consumed) {
        state = { phase: 'idle', turns: ['persisted result'], pending: null, undo: 'undo-a' };
        dataChangedCalls += 1;
      }
    }

    expect(state).toEqual({
      phase: 'idle',
      turns: ['persisted result'],
      pending: null,
      undo: 'undo-a',
    });
    expect(dataChangedCalls).toBe(1);
    expect(locks.has(1)).toBe(false);
  });
});

describe('buildChatRequestContext', () => {
  const pageContext: PilotPageContext = {
    view: 'applications-list',
    label: '投递列表',
    entity: { kind: 'application', id: '42', label: '星海科技 · 前端工程师' },
  };

  it('sends only request page context for an existing conversation', () => {
    expect(
      buildChatRequestContext({
        conversationId: 7,
        offerApplicationId: 99,
        offerId: 8,
        pageContext,
      }),
    ).toEqual({ page_context: pageContext });
  });

  it('sends the latest derived context while retaining same-page removals', () => {
    const identity = pageContextKey(pageContext);
    const removalState = pilotPageContextRemovalReducer(
      createPilotPageContextRemovalState(identity),
      { type: 'remove', contextKey: identity, chipKey: 'entity' },
    );
    const refreshedContext = {
      ...pageContext,
      label: '最新投递列表',
      entity: {
        ...pageContext.entity!,
        label: '星海智能 · 高级前端工程师',
        description: '当前状态：已录用',
      },
    };
    const activePageContext = deriveActivePageContext(refreshedContext, removalState);

    expect(buildChatRequestContext({ conversationId: 7, pageContext: activePageContext })).toEqual({
      page_context: { view: 'applications-list', label: '最新投递列表' },
    });
  });

  it('prefers a loaded offer application for a new conversation', () => {
    expect(
      buildChatRequestContext({
        offerApplicationId: 99,
        offerId: 8,
        pageContext,
      }),
    ).toEqual({
      context_type: 'application',
      context_ref: 99,
      mode: 'nego_coach',
      page_context: pageContext,
    });
  });

  it('binds a new general conversation to the page application entity', () => {
    expect(buildChatRequestContext({ pageContext })).toEqual({
      context_type: 'application',
      context_ref: '42',
      mode: 'general',
      page_context: pageContext,
    });
  });

  it('falls back to negotiation workspace context when only an offer id is available', () => {
    expect(buildChatRequestContext({ offerId: 8 })).toEqual({
      context_type: 'workspace',
      context_ref: '',
      mode: 'nego_coach',
    });
  });

  it('uses general workspace context for other new conversations', () => {
    expect(buildChatRequestContext({})).toEqual({
      context_type: 'workspace',
      context_ref: '',
      mode: 'general',
    });
  });
});

describe('confirmation retry intent', () => {
  it('preserves approval edits exactly', () => {
    const input = {
      approved: true as const,
      confirmation_token: 'a'.repeat(64),
      edited_args: { status: 'closed', closed_reason: 'Paused' },
    };

    expect(confirmationInputForRetry(input)).toEqual(input);
  });

  it('preserves rejection feedback exactly', () => {
    const input = {
      approved: false as const,
      confirmation_token: 'b'.repeat(64),
      rejection_feedback: 'Keep the current status.',
    };

    expect(confirmationInputForRetry(input)).toEqual(input);
  });

  it('forces state refresh for stale and in-progress confirmation errors', () => {
    expect(confirmationErrorRequiresSync('stale_pending_action')).toBe(true);
    expect(confirmationErrorRequiresSync('confirmation_in_progress')).toBe(true);
    expect(confirmationErrorRequiresSync('ai_provider_error')).toBe(false);
  });

  it('allows rejected confirmation edits to restore the review card immediately', () => {
    expect(confirmationErrorAllowsImmediateRetry('http_422')).toBe(true);
    expect(confirmationErrorAllowsImmediateRetry('confirmation_in_progress')).toBe(false);
  });
});

function msg(patch: Partial<ChatMessage> & Pick<ChatMessage, 'role'>): ChatMessage {
  return {
    id: patch.id ?? 1,
    conversation_id: patch.conversation_id ?? 1,
    role: patch.role,
    content: patch.content ?? '',
    tool_calls: patch.tool_calls,
    tool_call_id: patch.tool_call_id,
    created_at: patch.created_at ?? '2026-07-06T12:00:00+08:00',
  };
}

describe('buildTurns evidence normalization', () => {
  it('uses the correct status domain in Offer and application task summaries', () => {
    for (const [name, expected] of [['create_offer', '待处理'], ['update_offer', '待处理'], ['update_application_status', '待投递']]) {
      const turns = buildTurns([
        msg({ role: 'user', content: '更新状态' }),
        msg({ role: 'assistant', tool_calls: JSON.stringify([{ id: 'status-write', name, args: { status: 'pending' } }]) }),
      ]);
      expect(turns.flatMap((turn) => turn.steps ?? [])[0].detail).toBe(expected);
    }
  });

  it('uses Offer status labels without confusing pending with an unsent application', () => {
    for (const [status, label] of Object.entries({ pending: '待处理', negotiating: '谈判中', accepted: '已接受', declined: '已拒绝', expired: '已过期' })) {
      const turns = buildTurns([
        msg({ role: 'user', content: '查看 Offer' }),
        msg({ role: 'assistant', tool_calls: JSON.stringify([{ id: 'offer-read', name: 'list_offers', args: {} }]) }),
        msg({ role: 'tool', tool_call_id: 'offer-read', content: JSON.stringify([{ id: 2, company_name: '远帆科技', position_name: '工程师', total_cash: 400000, status }]) }),
      ]);
      const evidence = collectEvidence(turns);
      expect(evidence[0].meta).toContain(label);
      expect(evidence[0].meta).not.toContain('待投递');
    }
  });

  it('has localized metadata for resume match read tools', () => {
    const meta = toolMeta('list_resume_matches');

    expect(meta.kind).toBe('read');
    expect(meta.label).toBe('查看简历匹配记录');
  });

  it('has write metadata for resume v0.1 tools', () => {
    expect(toolMeta('resume_update_career_intent')).toMatchObject({
      kind: 'write',
      label: '更新简历求职意向',
    });
    expect(toolMeta('resume_rewrite_highlight')).toMatchObject({
      kind: 'write',
      label: '改写简历亮点',
    });
  });

  it('reloads stored turns for pending confirmations so current-turn evidence is available', async () => {
    const turns = await reloadConversationTurns(42, async (id) => {
      expect(id).toBe(42);
      return [
        { id: 1, conversation_id: 42, role: 'user', content: '更新启明智能 offer', created_at: '2026-01-01T00:00:00Z' },
        {
          id: 2,
          conversation_id: 42,
          role: 'assistant',
          content: '',
          tool_calls: JSON.stringify([{ id: 'call_1', function: { name: 'list_offers', arguments: '{"company_name":"启明智能"}' } }]),
          created_at: '2026-01-01T00:00:01Z',
        },
        {
          id: 3,
          conversation_id: 42,
          role: 'tool',
          content: JSON.stringify([{ id: 7, company_name: '启明智能', position_name: '算法工程师', total_cash: 1000000 }]),
          tool_call_id: 'call_1',
          created_at: '2026-01-01T00:00:02Z',
        },
        { id: 4, conversation_id: 42, role: 'assistant', content: '已找到 offer，需要确认。', created_at: '2026-01-01T00:00:03Z' },
      ];
    });

    expect(turns).not.toBeNull();
    expect(collectEvidence(turns ?? []).map((item) => item.title)).toEqual(['启明智能']);
  });

  it('keeps pending confirmation fallback available if stored turns cannot reload', async () => {
    const turns = await reloadConversationTurns(42, async () => {
      throw new Error('offline');
    });

    expect(turns).toBeNull();
  });

  it('keeps read evidence visible when the latest assistant turn is a pending write', () => {
    const turns = buildTurns([
      msg({ role: 'user', content: '把启明智能改成 offer' }),
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'read-app', name: 'get_application', args: { id: 7 } }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'read-app',
        content: JSON.stringify({
          id: 7,
          company_name: '启明智能',
          position_name: '算法工程师',
          status: 'interview',
          notes: '终面已结束。',
        }),
      }),
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'write-app', name: 'update_application_status', args: { id: 7, status: 'offer' } },
        ]),
      }),
    ]);

    expect(collectEvidence(turns).map((item) => item.title)).toEqual(['启明智能']);
    expect(turns[turns.length - 1]?.steps?.map((step) => step.name)).toEqual([
      'get_application',
      'update_application_status',
    ]);
  });

  it('localizes application status details in tool step summaries', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'write-app', name: 'create_application', args: { company_name: '牛客网', position_name: 'agent开发', status: 'applied' } },
        ]),
      }),
    ]);

    expect(turns[0].steps?.[0]).toMatchObject({
      name: 'create_application',
      detail: '已投递',
    });
  });

  it('attaches application evidence from tool results to the assistant turn', () => {
    const turns = buildTurns([
      msg({ role: 'user', content: '查看投递' }),
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ name: 'list_applications', args: {} }]),
      }),
      msg({
        role: 'tool',
        content: JSON.stringify([
          {
            id: 7,
            company_name: '字节跳动',
            position_name: '后端工程师',
            status: 'interview',
            source: 'manual',
            applied_at: '2026-07-01',
          },
        ]),
      }),
      msg({ role: 'assistant', content: '你有 1 条进行中的面试。' }),
    ]);

    expect(turns[1].steps?.[0]).toMatchObject({
      name: 'list_applications',
      detail: '字节跳动',
      evidence: [
        {
          id: 'application-7',
          kind: 'application',
          title: '字节跳动',
          meta: '后端工程师 \u00b7 面试 \u00b7 2026-07-01',
          source: 'list_applications',
        },
      ],
    });
  });

  it('targets application evidence from a real numeric application id', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-app', name: 'list_applications', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-app',
        content: JSON.stringify([{ id: 7, company_name: 'Acme', position_name: 'Engineer' }]),
      }),
      msg({ role: 'assistant', content: 'I found an application.' }),
    ]);

    expect(turns[0].steps?.[0].evidence?.[0]?.target).toEqual({ kind: 'application', id: 7 });
  });

  it('classifies event tool results as event evidence even when company is present', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-events', name: 'list_application_events', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-events',
        content: JSON.stringify([
          {
            record_type: 'application_event',
            application_event_id: 1,
            id: 1,
            application_id: 7,
            company_name: '拼多多',
            position_name: 'agent开发',
            event_type: 'interview',
            subtype: 'technical',
            scheduled_at: '2026-07-01T07:00:00Z',
            duration_minutes: 60,
            notes: '一面',
          },
        ]),
      }),
      msg({ role: 'assistant', content: '找到 1 场面试。' }),
    ]);

    expect(turns[0].steps?.[0]).toMatchObject({
      name: 'list_application_events',
      detail: '拼多多',
      evidence: [
        {
          id: 'list_application_events-1',
          kind: 'event',
          title: '拼多多',
          meta: 'agent开发 · interview · technical · 2026-07-01T07:00:00Z',
          snippet: '一面',
          source: 'list_application_events',
        },
      ],
    });
  });

  it('targets event evidence with its structured id and scheduled time', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-event', name: 'list_application_events', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-event',
        content: JSON.stringify([
          {
            record_type: 'application_event',
            application_event_id: 1,
            id: 9,
            company_name: 'Acme',
            scheduled_at: '2026-07-01T07:00:00Z',
          },
        ]),
      }),
      msg({ role: 'assistant', content: 'I found an event.' }),
    ]);

    expect(turns[0].steps?.[0].evidence?.[0]?.target).toEqual({
      kind: 'event',
      id: 1,
      scheduledAt: '2026-07-01T07:00:00Z',
    });
  });

  it('falls back to an event row id when application_event_id is absent', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-event', name: 'list_application_events', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-event',
        content: JSON.stringify([
          {
            record_type: 'application_event',
            id: 9,
            company_name: 'Acme',
            scheduled_at: '2026-07-01T07:00:00Z',
          },
        ]),
      }),
      msg({ role: 'assistant', content: 'I found an event.' }),
    ]);

    expect(turns[0].steps?.[0].evidence?.[0]?.target).toEqual({
      kind: 'event',
      id: 9,
      scheduledAt: '2026-07-01T07:00:00Z',
    });
  });

  it('leaves events with negative, fractional, unsafe, or non-finite ids targetless', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'call-negative', name: 'list_application_events', args: {} },
          { id: 'call-fractional', name: 'list_application_events', args: {} },
          { id: 'call-unsafe', name: 'list_application_events', args: {} },
          { id: 'call-nonfinite', name: 'list_application_events', args: {} },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-negative',
        content: JSON.stringify([
          { record_type: 'application_event', id: -1, company_name: 'Acme', scheduled_at: '2026-07-01T07:00:00Z' },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-fractional',
        content: JSON.stringify([
          { record_type: 'application_event', id: 1.5, company_name: 'Acme', scheduled_at: '2026-07-01T07:00:00Z' },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-unsafe',
        content: JSON.stringify([
          {
            record_type: 'application_event',
            id: Number.MAX_SAFE_INTEGER + 1,
            company_name: 'Acme',
            scheduled_at: '2026-07-01T07:00:00Z',
          },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-nonfinite',
        content: '[{"record_type":"application_event","id":1e309,"company_name":"Acme","scheduled_at":"2026-07-01T07:00:00Z"}]',
      }),
      msg({ role: 'assistant', content: 'These events are not safely targetable.' }),
    ]);

    expect(turns[0].steps?.map((step) => step.evidence?.[0]?.target)).toEqual([
      undefined,
      undefined,
      undefined,
      undefined,
    ]);
  });

  it('attaches evidence to the final answer when a tool-calling assistant includes preamble content', () => {
    const turns = buildTurns([
      msg({ role: 'user', content: '查看投递' }),
      msg({
        role: 'assistant',
        content: '我来查一下。',
        tool_calls: JSON.stringify([{ id: 'call-apps', name: 'list_applications', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-apps',
        content: JSON.stringify([
          {
            id: 7,
            company_name: '字节跳动',
            position_name: '后端工程师',
            status: 'interview',
          },
        ]),
      }),
      msg({ role: 'assistant', content: '你有 1 条进行中的面试。' }),
    ]);

    expect(turns.map((turn) => [turn.role, turn.content, turn.steps?.length ?? 0])).toEqual([
      ['user', '查看投递', 0],
      ['assistant', '我来查一下。', 0],
      ['assistant', '你有 1 条进行中的面试。', 1],
    ]);
    expect(collectEvidence(turns).map((item) => item.title)).toEqual(['字节跳动']);
  });

  it('keeps malformed tool results as an unavailable detail instead of throwing', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ name: 'search_knowledge', args: { query: '系统设计' } }]),
      }),
      msg({ role: 'tool', content: '{bad json' }),
      msg({ role: 'assistant', content: '已搜索。' }),
    ]);

    expect(turns[0].steps?.[0]).toMatchObject({
      name: 'search_knowledge',
      detail: '系统设计',
      evidenceUnavailable: true,
    });
  });

  it('shows empty array tool results as no matching records instead of unavailable evidence', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ name: 'search_knowledge', args: { query: 'negotiation' } }]),
      }),
      msg({ role: 'tool', content: '[]' }),
      msg({ role: 'assistant', content: 'No knowledge matched.' }),
    ]);

    expect(turns[0].steps?.[0]).toMatchObject({
      name: 'search_knowledge',
      detail: 'negotiation',
      resultText: '没有匹配结果',
    });
    expect(turns[0].steps?.[0].evidenceUnavailable).toBeFalsy();
  });

  it('attaches evidence from knowledge search result payloads', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-knowledge', name: 'search_knowledge', args: { query: 'JVM' } }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-knowledge',
        content: JSON.stringify([
          {
            record_type: 'knowledge_search_result',
            search_result_id: 12,
            document_id: 3,
            document_title: 'JVM内存模型',
            source_name: 'manual',
            chunk_id: 12,
            chunk_index: 0,
            snippet: '堆、栈、方法区和程序计数器是常见考点。',
          },
        ]),
      }),
      msg({ role: 'assistant', content: '找到了 JVM 笔记。' }),
    ]);

    expect(turns[0].steps?.[0]).toMatchObject({
      name: 'search_knowledge',
      detail: 'JVM内存模型',
      evidence: [
        {
          id: 'search_knowledge-12',
          kind: 'knowledge',
          title: 'JVM内存模型',
          meta: 'manual',
          snippet: '堆、栈、方法区和程序计数器是常见考点。',
          source: 'search_knowledge',
        },
      ],
    });
    expect(turns[0].steps?.[0].evidenceUnavailable).toBeFalsy();
  });

  it('keeps knowledge document contents as compact previews in tool evidence', () => {
    const longContent = [
      '# Java Threads',
      'Processes isolate memory while threads share process resources.',
      'Thread creation can use Thread, Runnable, Callable, or a thread pool.',
      'This sentence should not be visible in the process timeline preview.',
    ].join('\n\n');

    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-doc', name: 'get_knowledge_document', args: { id: 8 } }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-doc',
        content: JSON.stringify({
          record_type: 'knowledge_document',
          knowledge_document_id: 8,
          id: 8,
          title: 'Java Threads',
          content: longContent,
        }),
      }),
      msg({ role: 'assistant', content: 'I checked the document.' }),
    ]);

    const snippet = turns[0].steps?.[0].evidence?.[0].snippet ?? '';
    expect(snippet.length).toBeLessThanOrEqual(180);
    expect(snippet).toContain('Processes isolate memory');
    expect(snippet).not.toContain('This sentence should not be visible');
  });

  it('classifies resume tool results as resume evidence', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-resume', name: 'list_resumes', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-resume',
        content: JSON.stringify([
          {
            record_type: 'resume',
            resume_id: 6,
            id: 6,
            name: '后端简历',
            parse_status: 'text-ready',
            parsed_data: 'Java Spring Boot 高并发项目经验',
          },
        ]),
      }),
      msg({ role: 'assistant', content: '找到了简历。' }),
    ]);

    expect(turns[0].steps?.[0]).toMatchObject({
      name: 'list_resumes',
      detail: '后端简历',
      evidence: [
        {
          id: 'list_resumes-6',
          kind: 'resume',
          title: '后端简历',
          meta: 'text-ready',
          snippet: 'Java Spring Boot 高并发项目经验',
          source: 'list_resumes',
        },
      ],
    });
  });

  it('targets direct resume evidence from its real numeric id', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-resume', name: 'list_resumes', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-resume',
        content: JSON.stringify([{ record_type: 'resume', id: 6, name: 'Backend resume' }]),
      }),
      msg({ role: 'assistant', content: 'I found a resume.' }),
    ]);

    expect(turns[0].steps?.[0].evidence?.[0]?.target).toEqual({ kind: 'resume', id: 6 });
  });

  it('classifies resume match results as match evidence instead of generic resume evidence', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-match', name: 'list_resume_matches', args: { resume_id: 6 } }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-match',
        content: JSON.stringify([
          {
            record_type: 'resume_match',
            resume_match_id: 9,
            id: 9,
            resume_id: 6,
            application_id: 7,
            jd_text: '后端开发工程师，负责高并发交易系统',
            result: '{"summary":"匹配度较高，后端项目经验契合"}',
          },
        ]),
      }),
      msg({ role: 'assistant', content: '找到了简历匹配结果。' }),
    ]);

    expect(turns[0].steps?.[0]).toMatchObject({
      name: 'list_resume_matches',
      detail: '简历匹配 #9',
      evidence: [
        {
          id: 'list_resume_matches-9',
          kind: 'resume',
          title: '简历匹配 #9',
          meta: '简历 #6 · 投递 #7',
          snippet: '匹配度较高，后端项目经验契合',
          source: 'list_resume_matches',
        },
      ],
    });
  });

  it('targets resume match evidence using resume_id instead of the match row id', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-match', name: 'list_resume_matches', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-match',
        content: JSON.stringify([
          { record_type: 'resume_match', id: 9, resume_match_id: 9, resume_id: 6 },
        ]),
      }),
      msg({ role: 'assistant', content: 'I found a resume match.' }),
    ]);

    expect(turns[0].steps?.[0].evidence?.[0]?.target).toEqual({ kind: 'resume', id: 6 });
  });

  it('targets offer evidence from its real numeric id', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-offer', name: 'list_offers', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-offer',
        content: JSON.stringify([{ id: 3, company_name: 'Acme', total_cash: 600000 }]),
      }),
      msg({ role: 'assistant', content: 'I found an offer.' }),
    ]);

    expect(turns[0].steps?.[0].evidence?.[0]?.target).toEqual({ kind: 'offer', id: 3 });
  });

  it('leaves malformed, string, or non-positive record ids targetless', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'call-app', name: 'list_applications', args: {} },
          { id: 'call-offer', name: 'list_offers', args: {} },
          { id: 'call-resume', name: 'list_resumes', args: {} },
          { id: 'call-event', name: 'list_application_events', args: {} },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-app',
        content: JSON.stringify([{ id: '7', company_name: 'Acme' }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-offer',
        content: JSON.stringify([{ id: 0, company_name: 'Acme', total_cash: 600000 }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-resume',
        content: JSON.stringify([{ record_type: 'resume', id: '6', name: 'Backend resume' }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-event',
        content: JSON.stringify([
          {
            record_type: 'application_event',
            application_event_id: '1',
            id: '9',
            company_name: 'Acme',
            scheduled_at: '2026-07-01T07:00:00Z',
          },
        ]),
      }),
      msg({ role: 'assistant', content: 'These records are not safely targetable.' }),
    ]);

    for (const step of turns[0].steps ?? []) {
      expect(step.evidence?.[0]?.target).toBeUndefined();
    }
  });

  it('leaves event evidence without a valid scheduled time targetless', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'call-missing-time', name: 'list_application_events', args: {} },
          { id: 'call-invalid-time', name: 'list_application_events', args: {} },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-missing-time',
        content: JSON.stringify([
          { record_type: 'application_event', application_event_id: 1, company_name: 'Acme' },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-invalid-time',
        content: JSON.stringify([
          {
            record_type: 'application_event',
            application_event_id: 2,
            company_name: 'Acme',
            scheduled_at: 'not-a-date',
          },
        ]),
      }),
      msg({ role: 'assistant', content: 'These events are not safely targetable.' }),
    ]);

    for (const step of turns[0].steps ?? []) {
      expect(step.evidence?.[0]?.target).toBeUndefined();
    }
  });

  it('leaves event evidence with an empty scheduled time targetless', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-empty-time', name: 'list_application_events', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-empty-time',
        content: JSON.stringify([
          {
            record_type: 'application_event',
            id: 1,
            company_name: 'Acme',
            scheduled_at: '   ',
          },
        ]),
      }),
      msg({ role: 'assistant', content: 'This event is not safely targetable.' }),
    ]);

    expect(turns[0].steps?.[0].evidence?.[0]?.target).toBeUndefined();
  });

  it('leaves events with rollover calendar or time values targetless', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'call-rollover-date', name: 'list_application_events', args: {} },
          { id: 'call-rollover-hour', name: 'list_application_events', args: {} },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-rollover-date',
        content: JSON.stringify([
          {
            record_type: 'application_event',
            id: 1,
            company_name: 'Acme',
            scheduled_at: '2026-02-30T09:05:00Z',
          },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-rollover-hour',
        content: JSON.stringify([
          {
            record_type: 'application_event',
            id: 2,
            company_name: 'Acme',
            scheduled_at: '2026-01-01T24:00:00Z',
          },
        ]),
      }),
      msg({ role: 'assistant', content: 'These events are not safely targetable.' }),
    ]);

    expect(turns[0].steps?.map((step) => step.evidence?.[0]?.target)).toEqual([undefined, undefined]);
  });

  it('keeps unsupported, plain-text, and malformed results targetless', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'call-knowledge', name: 'search_knowledge', args: {} },
          { id: 'call-jd', name: 'list_jd_analyses', args: {} },
          { id: 'call-note', name: 'list_notes', args: {} },
          { id: 'call-unknown', name: 'unknown_tool', args: {} },
          { id: 'call-unknown-application', name: 'unknown_application_reader', args: {} },
          { id: 'call-text', name: 'tool_text', args: {} },
          { id: 'call-json', name: 'tool_json', args: {} },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-knowledge',
        content: JSON.stringify([{ record_type: 'knowledge_document', id: 4, title: 'Knowledge' }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-jd',
        content: JSON.stringify([{ record_type: 'jd_analysis', id: 5, jd_text: 'Role details' }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-note',
        content: JSON.stringify([{ id: 6, title: 'A note', company_name: 'Not an application' }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-unknown',
        content: JSON.stringify([{ record_type: 'unknown', id: 7, company_name: 'Not an application' }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-unknown-application',
        content: JSON.stringify([{ id: 7, company_name: 'Not an application' }]),
      }),
      msg({ role: 'tool', tool_call_id: 'call-text', content: 'A plain text result.' }),
      msg({ role: 'tool', tool_call_id: 'call-json', content: '{bad json' }),
      msg({ role: 'assistant', content: 'Only structured local records can be targets.' }),
    ]);

    expect(turns[0].steps?.map((step) => step.evidence?.[0]?.target)).toEqual([
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
    ]);
  });

  it('attaches evidence from JD analysis payloads', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-jd', name: 'list_jd_analyses', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-jd',
        content: JSON.stringify([
          {
            record_type: 'jd_analysis',
            jd_analysis_id: 2,
            id: 2,
            application_id: 7,
            jd_source: 'text',
            jd_text: '后端开发工程师，负责高并发交易系统',
            result: '{"summary":"高并发后端岗位，重点关注 Java、缓存和分布式系统"}',
          },
        ]),
      }),
      msg({ role: 'assistant', content: '找到了 JD 分析。' }),
    ]);

    expect(turns[0].steps?.[0]).toMatchObject({
      name: 'list_jd_analyses',
      detail: 'JD 分析 #2',
      evidence: [
        {
          id: 'list_jd_analyses-2',
          kind: 'jd',
          title: 'JD 分析 #2',
          meta: 'text · 投递 #7',
          snippet: '高并发后端岗位，重点关注 Java、缓存和分布式系统',
          source: 'list_jd_analyses',
        },
      ],
    });
    expect(turns[0].steps?.[0].evidenceUnavailable).toBeFalsy();
  });

  it('matches multiple tool results to the correct tool call id', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'call-apps', name: 'list_applications', args: {} },
          { id: 'call-knowledge', name: 'search_knowledge', args: { query: '系统设计' } },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-knowledge',
        content: JSON.stringify([{ id: 10, title: '系统设计', summary: '模式' }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-apps',
        content: JSON.stringify([{ id: 7, company_name: '字节跳动', position_name: '后端工程师' }]),
      }),
      msg({ role: 'assistant', content: '都找到了。' }),
    ]);

    expect(turns[0].steps?.map((step) => [step.name, step.detail])).toEqual([
      ['list_applications', '字节跳动'],
      ['search_knowledge', '系统设计'],
    ]);
    expect(turns[0].steps?.[0].evidence?.[0]).toMatchObject({
      id: 'application-7',
      source: 'list_applications',
      title: '字节跳动',
    });
    expect(turns[0].steps?.[1].evidence?.[0]).toMatchObject({
      id: 'search_knowledge-10',
      source: 'search_knowledge',
      title: '系统设计',
    });
  });

  it('maps legacy tool results to pending steps in result order when ids are missing', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { name: 'list_applications', args: {} },
          { name: 'search_knowledge', args: { query: '系统设计' } },
        ]),
      }),
      msg({
        role: 'tool',
        content: JSON.stringify([{ id: 7, company_name: '字节跳动', position_name: '后端工程师' }]),
      }),
      msg({
        role: 'tool',
        content: JSON.stringify([{ id: 10, title: '系统设计', summary: '模式' }]),
      }),
      msg({ role: 'assistant', content: '都找到了。' }),
    ]);

    expect(turns[0].steps?.map((step) => [step.name, step.detail])).toEqual([
      ['list_applications', '字节跳动'],
      ['search_knowledge', '系统设计'],
    ]);
  });

  it('maps missing-id results to the next unfilled step after id-based results', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'call-apps', name: 'list_applications', args: {} },
          { id: 'call-knowledge', name: 'search_knowledge', args: { query: '系统设计' } },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-apps',
        content: JSON.stringify([{ id: 7, company_name: '字节跳动', position_name: '后端工程师' }]),
      }),
      msg({
        role: 'tool',
        content: JSON.stringify([{ id: 10, title: '系统设计', summary: '模式' }]),
      }),
      msg({ role: 'assistant', content: '都找到了。' }),
    ]);

    expect(turns[0].steps?.map((step) => [step.name, step.detail])).toEqual([
      ['list_applications', '字节跳动'],
      ['search_knowledge', '系统设计'],
    ]);
    expect(turns[0].steps?.[0].evidence?.[0]).toMatchObject({
      id: 'application-7',
      title: '字节跳动',
    });
    expect(turns[0].steps?.[1].evidence?.[0]).toMatchObject({
      id: 'search_knowledge-10',
      title: '系统设计',
    });
  });

  it('does not overwrite id-matched empty evidence results with later fallback results', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([
          { id: 'call-note', name: 'search_knowledge', args: { query: '缺失来源' } },
          { id: 'call-apps', name: 'list_applications', args: {} },
        ]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-note',
        content: JSON.stringify({ ok: true }),
      }),
      msg({
        role: 'tool',
        content: JSON.stringify([{ id: 7, company_name: '字节跳动', position_name: '后端工程师' }]),
      }),
      msg({ role: 'assistant', content: '找到 1 条投递。' }),
    ]);

    expect(turns[0].steps?.map((step) => [step.name, step.detail])).toEqual([
      ['search_knowledge', '缺失来源'],
      ['list_applications', '字节跳动'],
    ]);
    expect(turns[0].steps?.[0].evidence).toBeUndefined();
    expect(turns[0].steps?.[1].evidence?.[0]).toMatchObject({
      id: 'application-7',
      title: '字节跳动',
    });
  });

  it('aggregates newest evidence first across visible turns', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ name: 'list_offers', args: {} }]),
      }),
      msg({
        role: 'tool',
        content: JSON.stringify([{ id: 3, company_name: '启明智能', position_name: '产品经理', total_cash: 600000 }]),
      }),
      msg({ role: 'assistant', content: '找到了 offer。' }),
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ name: 'list_applications', args: {} }]),
      }),
      msg({
        role: 'tool',
        content: JSON.stringify([{ id: 4, company_name: '星河智能', position_name: '产品经理', status: 'applied' }]),
      }),
      msg({ role: 'assistant', content: '找到了投递。' }),
    ]);

    expect(collectEvidence(turns).map((item) => item.title)).toEqual(['星河智能', '启明智能']);
  });

  it('preserves backend evidence order within a single tool result', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ name: 'list_applications', args: {} }]),
      }),
      msg({
        role: 'tool',
        content: JSON.stringify([
          { id: 5, company_name: '启明智能', position_name: '产品经理', status: 'interview' },
          { id: 4, company_name: '星河智能', position_name: '产品经理', status: 'applied' },
        ]),
      }),
      msg({ role: 'assistant', content: '找到了多条投递。' }),
    ]);

    expect(collectEvidence(turns).map((item) => item.title)).toEqual(['启明智能', '星河智能']);
  });

  it('keeps plain-text tool errors visible in the process timeline data', () => {
    const turns = buildTurns([
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-app', name: 'get_application', args: {} }]),
      }),
      msg({ role: 'tool', tool_call_id: 'call-app', content: "错误：'id'" }),
      msg({ role: 'assistant', content: 'I could not load it.' }),
    ]);

    expect(turns[0].steps?.[0]).toMatchObject({
      name: 'get_application',
      resultText: "错误：'id'",
    });
    expect(turns[0].steps?.[0].evidenceUnavailable).toBeFalsy();
  });

  it('returns the persisted pending action for a selected conversation', () => {
    const pending = pendingActionForConversation(
      [
        {
          id: 42,
          title: 'Offer',
          context_type: 'workspace',
          context_ref: '',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          pending_action: {
            tool_name: 'update_application_status',
            human: '更新状态',
            confirmation_token: 'a'.repeat(64),
            args: { id: 1, status: 'offer' },
          },
        },
      ],
      42,
    );

    expect(pending).toEqual({
      tool_name: 'update_application_status',
      human: '更新状态',
      confirmation_token: 'a'.repeat(64),
      args: { id: 1, status: 'offer' },
    });
  });

  it('explains the next step while a chained write confirmation is pending', () => {
    expect(
      pendingComposerDisabledReason({
        tool_name: 'create_application',
        human: '新建投递：牛客网 - 软件测试工程师',
        confirmation_token: 'a'.repeat(64),
        workflow: {
          current_step: 1,
          total_steps: 2,
          current_label: '新建投递',
          next_label: '保存面试复盘',
        },
      }),
    ).toBe('请先确认“新建投递”，确认后我会继续保存面试复盘。');
  });

  it('hydrates a missing pending action for the active conversation after refresh', () => {
    const persisted = {
      tool_name: 'create_application',
      human: '新建投递：牛客网 - 软件测试工程师',
      confirmation_token: 'a'.repeat(64),
      args: { company_name: '牛客网', position_name: '软件测试工程师', status: 'interview' },
    };

    const pending = hydrateMissingPendingAction(
      null,
      [
        {
          id: 65,
          title: '牛客网面试复盘',
          context_type: 'workspace',
          context_ref: '',
          created_at: '2026-07-09T00:00:00Z',
          updated_at: '2026-07-09T00:00:01Z',
          pending_action: persisted,
        },
      ],
      65,
    );

    expect(pending).toEqual(persisted);
  });

  it('keeps the current pending action while conversation refresh is stale', () => {
    const current = {
      tool_name: 'create_application_event',
      human: '新建投递事件',
      confirmation_token: 'b'.repeat(64),
      args: { application_id: 40, event_type: 'written_test' },
    };

    const pending = hydrateMissingPendingAction(
      current,
      [
        {
          id: 65,
          title: '牛客网面试复盘',
          context_type: 'workspace',
          context_ref: '',
          created_at: '2026-07-09T00:00:00Z',
          updated_at: '2026-07-09T00:00:01Z',
          pending_action: {
            tool_name: 'create_application',
            human: '新建投递：牛客网 - 软件测试工程师',
            confirmation_token: 'a'.repeat(64),
            args: { company_name: '牛客网', position_name: '软件测试工程师' },
          },
        },
      ],
      65,
    );

    expect(pending).toEqual(current);
  });

});

describe('Pilot turn presentation reconstruction', () => {
  it('parses persisted markdown headings and preserves leading detail markdown', () => {
    const presentation = parseTurnPresentation([
      '# 投递进展',
      '',
      '已核对 **两个**岗位的状态。',
      '',
      '   ## 结论',
      '',
      '优先准备下一轮技术面。',
      '',
      '### 下一步',
      '',
      '-  整理项目亮点  ',
      '- 联系招聘方确认时间',
    ].join('\n'));

    expect(presentation).toEqual({
      conclusion: '优先准备下一轮技术面。',
      actions: ['整理项目亮点', '联系招聘方确认时间'],
      detailMarkdown: '# 投递进展\n\n已核对 **两个**岗位的状态。',
    });
  });

  it('falls back for incomplete or reversed presentation headings', () => {
    const onlyConclusion = '## 结论\n\n继续跟进。';
    const noAction = '## 结论\n\n继续跟进。\n\n## 下一步\n\n稍后处理。';
    const reversed = '## 下一步\n\n- 跟进\n\n## 结论\n\n继续跟进。';

    expect(parseTurnPresentation(onlyConclusion)).toBeUndefined();
    expect(parseTurnPresentation(noAction)).toBeUndefined();
    expect(parseTurnPresentation(reversed)).toBeUndefined();

    const turns = buildTurns([msg({ role: 'assistant', content: noAction })]);
    expect(turns[0]).toMatchObject({
      content: noAction,
      taskTitle: '本轮任务',
      presentation: undefined,
    });
  });

  it('keeps at most the first three actionable bullets', () => {
    const presentation = parseTurnPresentation([
      '## 结论',
      '',
      '可以推进。',
      '',
      '## 下一步',
      '',
      '* 第一项',
      '+ 第二项',
      '- 第三项',
      '- 第四项',
    ].join('\n'));

    expect(presentation?.actions).toEqual(['第一项', '第二项', '第三项']);
  });

  it('attaches real tool steps and structured presentation to the final assistant turn', () => {
    const turns = buildTurns([
      msg({
        role: 'user',
        content: '   Review    application  pipeline and prepare an exceptionally detailed final summary today  ',
      }),
      msg({
        role: 'assistant',
        tool_calls: JSON.stringify([{ id: 'call-apps', name: 'list_applications', args: {} }]),
      }),
      msg({
        role: 'tool',
        tool_call_id: 'call-apps',
        content: JSON.stringify([{ id: 7, company_name: 'OpenAI', position_name: 'Engineer' }]),
      }),
      msg({
        role: 'assistant',
        content: [
          '已查到投递记录。',
          '',
          '## 结论',
          '',
          '应优先准备 OpenAI 面试。',
          '',
          '## 下一步',
          '',
          '- 更新项目案例',
          '- 安排模拟面试',
        ].join('\n'),
      }),
    ]);

    expect(turns).toHaveLength(2);
    expect(turns[1]).toMatchObject({
      role: 'assistant',
      content: '已查到投递记录。',
      taskTitle: 'Review application pipeline and pre…',
      presentation: {
        conclusion: '应优先准备 OpenAI 面试。',
        actions: ['更新项目案例', '安排模拟面试'],
        detailMarkdown: '已查到投递记录。',
      },
      steps: [
        {
          name: 'list_applications',
          toolCallId: 'call-apps',
        },
      ],
    });
    expect(turns[1].steps).toHaveLength(1);
    expect(turns[1].taskTitle).toHaveLength(36);
  });

  it('stops actions at a following peer heading and retains nested list content with the trailing markdown', () => {
    const presentation = parseTurnPresentation([
      '前置说明。',
      '',
      '## 结论',
      '',
      '先完成面试准备。',
      '',
      '## 下一步',
      '',
      '- 完成项目案例',
      '  - 用量化指标补充成果',
      '- 约一次模拟面试',
      '',
      '## 备注',
      '',
      '提交后跟进反馈。',
      '- 这不是下一步行动',
    ].join('\n'));

    expect(presentation).toEqual({
      conclusion: '先完成面试准备。',
      actions: ['完成项目案例\n  - 用量化指标补充成果', '约一次模拟面试'],
      detailMarkdown: '前置说明。\n\n## 备注\n\n提交后跟进反馈。\n- 这不是下一步行动',
    });
  });

  it('preserves root prose and fenced markdown while retaining nested action continuations', () => {
    const presentation = parseTurnPresentation([
      '## 结论',
      '',
      '先完成准备。',
      '',
      '## 下一步',
      '',
      '- 执行 A',
      '  - 保留的嵌套延续',
      '补充说明',
      '```markdown',
      '- 这是围栏示例，不是行动',
      '```',
    ].join('\n'));

    expect(presentation?.actions).toEqual(['执行 A\n  - 保留的嵌套延续']);
    expect(presentation?.detailMarkdown).toContain('补充说明');
    expect(presentation?.detailMarkdown).toContain('```markdown\n- 这是围栏示例，不是行动\n```');
  });

  it('does not parse fenced historical markdown examples as a Pilot presentation', () => {
    const historicalExample = [
      '旧回复模板：',
      '',
      '```markdown',
      '## 结论',
      '',
      '这只是示例结论。',
      '',
      '## 下一步',
      '',
      '- 这只是示例行动',
      '```',
    ].join('\n');

    expect(parseTurnPresentation(historicalExample)).toBeUndefined();
    expect(buildTurns([msg({ role: 'assistant', content: historicalExample })])[0]).toMatchObject({
      content: historicalExample,
      presentation: undefined,
    });
  });

  it('does not treat fenced list examples as additional actions', () => {
    const presentation = parseTurnPresentation([
      '## 结论',
      '',
      '按计划推进。',
      '',
      '## 下一步',
      '',
      '- 准备项目案例',
      '```markdown',
      '- 示例列表项',
      '* 另一条示例',
      '```',
      '- 安排模拟面试',
    ].join('\n'));

    expect(presentation?.actions).toEqual(['准备项目案例', '安排模拟面试']);
  });

  it('does not append nested fenced list examples to the current action', () => {
    const presentation = parseTurnPresentation([
      '## 结论',
      '',
      '按计划推进。',
      '',
      '## 下一步',
      '',
      '- 准备项目案例',
      '    ```markdown',
      '    ## 示例结论',
      '    - 示例列表项',
      '    ```',
      '- 安排模拟面试',
    ].join('\n'));

    expect(presentation?.actions).toEqual(['准备项目案例', '安排模拟面试']);
  });

  it('does not mistake root indented code for a fence that masks a later presentation', () => {
    const presentation = parseTurnPresentation([
      '    ```not-a-fence-at-root',
      '## 结论',
      '',
      '可以推进。',
      '',
      '## 下一步',
      '',
      '- 准备项目案例',
    ].join('\n'));

    expect(presentation).toEqual({
      conclusion: '可以推进。',
      actions: ['准备项目案例'],
      detailMarkdown: '```not-a-fence-at-root',
    });
  });

  it('does not initialize actions from space- or tab-indented code bullets', () => {
    const presentation = parseTurnPresentation([
      '## 结论',
      '',
      '需要先区分示例代码。',
      '',
      '## 下一步',
      '',
      '    - 四空格的示例代码',
      '\t- 制表符的示例代码',
      '- 真正的下一步',
    ].join('\n'));

    expect(presentation).toEqual({
      conclusion: '需要先区分示例代码。',
      actions: ['真正的下一步'],
      detailMarkdown: '    - 四空格的示例代码\n\t- 制表符的示例代码',
    });
    expect(parseTurnPresentation('## 结论\n\n只含代码。\n\n## 下一步\n\n\t- 制表示例')).toBeUndefined();
  });

  it('keeps a root fence open when a four-space marker appears inside it', () => {
    const fencedExample = [
      '```markdown',
      '    ```',
      '## 结论',
      '',
      '这仍在围栏内。',
      '',
      '## 下一步',
      '',
      '- 这不是实际行动',
    ].join('\n');

    expect(parseTurnPresentation(fencedExample)).toBeUndefined();
    expect(buildTurns([msg({ role: 'assistant', content: fencedExample })])[0]).toMatchObject({
      content: fencedExample,
      presentation: undefined,
    });
  });

  it('closes a list-contained fence at the shallower content-start column', () => {
    const presentation = parseTurnPresentation([
      '- 代码示例容器',
      '    ```markdown',
      '    - 围栏内示例',
      '  ```',
      '## 结论',
      '',
      '围栏已结束。',
      '',
      '## 下一步',
      '',
      '- 执行真实行动',
    ].join('\n'));

    expect(presentation).toMatchObject({
      conclusion: '围栏已结束。',
      actions: ['执行真实行动'],
    });
  });

  it('closes a list-contained fence within three columns past its content start', () => {
    const presentation = parseTurnPresentation([
      '- 代码示例容器',
      '    ```markdown',
      '    - 围栏内示例',
      '     ```',
      '## 结论',
      '',
      '围栏已结束。',
      '',
      '## 下一步',
      '',
      '- 执行真实行动',
    ].join('\n'));

    expect(presentation).toMatchObject({
      conclusion: '围栏已结束。',
      actions: ['执行真实行动'],
    });
  });

  it('uses a list container even when its opening fence starts within three root columns', () => {
    const relativeClose = [
      '- 代码示例容器',
      '  ```markdown',
      '  - 围栏内示例',
      '     ```',
      '## 结论',
      '',
      '围栏已结束。',
      '',
      '## 下一步',
      '',
      '- 执行真实行动',
    ].join('\n');
    const tooDeepToClose = relativeClose.replace('     ```', '      ```');

    expect(parseTurnPresentation(relativeClose)).toMatchObject({
      conclusion: '围栏已结束。',
      actions: ['执行真实行动'],
    });
    expect(parseTurnPresentation(tooDeepToClose)).toBeUndefined();
  });

  it('truncates task titles at a code-point boundary within the 36-character display limit', () => {
    const turns = buildTurns([
      msg({ role: 'user', content: '😀'.repeat(37) }),
      msg({ role: 'assistant', content: '已处理。' }),
    ]);
    const taskTitle = turns[1].taskTitle;

    expect(taskTitle).toBe(`${'😀'.repeat(35)}…`);
    expect(Array.from(taskTitle ?? '')).toHaveLength(36);
  });
});
