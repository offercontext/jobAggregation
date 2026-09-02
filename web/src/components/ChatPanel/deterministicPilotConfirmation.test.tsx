// @vitest-environment jsdom
import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App as AntApp } from 'antd';
import ProposalCard from './ProposalCard';
import type { PendingAction } from '@/types/chat';

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
if (!HTMLElement.prototype.scrollIntoView) HTMLElement.prototype.scrollIntoView = vi.fn();

const chatState = vi.hoisted(() => ({
  streamChat: vi.fn(),
  getConversation: vi.fn().mockResolvedValue([]),
  getOffer: vi.fn().mockResolvedValue({ id: 17, application_id: 42 }),
  attachments: [] as Array<{ kind: 'application' | 'offer' | 'resume'; id: string; label: string }>,
}));

vi.mock('@/services/chat', () => ({
  streamChat: chatState.streamChat,
  streamConfirmAction: vi.fn(),
  getSettings: vi.fn().mockResolvedValue({ chat_auto_approve_writes: false }),
  SETTINGS_QUERY_KEY: ['settings'],
  updateAutoApprove: vi.fn(),
  listConversations: vi.fn().mockResolvedValue([]),
  getConversation: chatState.getConversation,
  deleteConversation: vi.fn(),
  updateConversation: vi.fn(),
  undoLastWrite: vi.fn(),
}));
vi.mock('@/services/offers', () => ({ getOffer: chatState.getOffer }));
vi.mock('@/services/onboarding', () => ({ ONBOARDING_QUERY_KEY: ['onboarding'] }));
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useQuery: () => ({ data: [], isLoading: false }),
}));
vi.mock('@/features/pilot/PilotAttachmentContext', () => ({
  usePilotAttachments: () => ({
    activeKey: undefined,
    attachments: chatState.attachments,
    notice: null,
    addAttachment: vi.fn(),
    removeAttachment: vi.fn(),
    setActiveConversationKey: vi.fn(),
    clearAttachmentsByKey: vi.fn(),
    beginNewAttachmentDraft: vi.fn(() => 'draft'),
    ensureNewAttachmentDraft: vi.fn(() => 'draft'),
  }),
}));
vi.mock('./ThreadRail', () => ({ default: () => null }));
vi.mock('./MessageBubble', () => ({ default: () => null }));
vi.mock('./ThinkingIndicator', () => ({ default: () => null }));
vi.mock('./Composer', () => ({
  default: (props: { draftValue?: string; onSend?: (value: string) => void }) => (
    <>
      <textarea data-testid="mock-pilot-composer" value={props.draftValue ?? ''} readOnly />
      <button
        type="button"
        data-testid="mock-pilot-send"
        onClick={() => props.onSend?.(props.draftValue ?? '')}
      >
        发送
      </button>
    </>
  ),
}));
vi.mock('./ContextAttachmentRail', () => ({ default: () => null }));
vi.mock('./NativePilotAttachmentDropSurface', () => ({ default: (props: { children?: ReactNode }) => <>{props.children}</> }));
vi.mock('./ContextPanel', () => ({ default: () => null }));
vi.mock('@/components/KanbanBoard/PilotContextDropTarget', () => ({ default: (props: { children?: ReactNode }) => <>{props.children}</> }));

const { default: ChatPanel, draftContextMatchesOfferScope } = await import('./index');

let root: Root | undefined;
let container: HTMLDivElement | undefined;

afterEach(() => {
  chatState.streamChat.mockReset();
  chatState.getConversation.mockClear();
  chatState.getOffer.mockClear();
  chatState.attachments = [];
  act(() => root?.unmount());
  container?.remove();
});

function renderProposal(action: PendingAction) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(
    <AntApp>
      <ProposalCard action={action} loading={false} evidence={[]} onConfirm={vi.fn()} onCancel={vi.fn()} />
    </AntApp>,
  ));
  return container;
}

const jdAction: PendingAction = {
  tool_name: 'save_application_jd_version',
  human: '请确认将这份岗位资料保存到当前投递。',
  confirmation_token: 'token',
  args: {
    application_id: 7,
    jd_text: '负责服务端系统设计与开发，参与稳定性建设。',
    source_url: 'https://example.invalid/job/7',
    expected_current_version_id: 3,
    idempotency_key: 'abcdefghijklmnop',
  },
  target: {
    id: 'application-7',
    kind: 'application',
    title: '示例公司',
    meta: '后端工程师',
    source: 'pending_action',
  },
  application_jd: {
    current_version_number: 3,
    proposed_version_number: 4,
  },
  editable_fields: [
    { field: 'jd_text', type: 'long_text' },
    { field: 'source_url', type: 'string', clearable: true, clear_value: null },
  ],
};

describe('deterministic Pilot JD confirmation card', () => {
  it('binds an unsent negotiation draft to exactly one Offer scope', () => {
    const request = {
      requestKey: 90,
      context_type: 'application' as const,
      context_ref: '42',
      context_label: '去哪儿旅行 · Agent 开发',
      mode: 'nego_coach' as const,
      attachments: [{ kind: 'offer' as const, id: '17', label: '去哪儿旅行 · Agent 开发' }],
      composerDraft: '谈薪草稿',
    };

    expect(draftContextMatchesOfferScope(request, 17)).toBe(true);
    expect(draftContextMatchesOfferScope(request, 18)).toBe(false);
    expect(draftContextMatchesOfferScope(request, undefined)).toBe(false);
  });

  it('accepts a negotiation composer prefill without starting Chat or SSE', async () => {
    const claim = vi.fn(() => true);
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(
      <AntApp>
        <ChatPanel
          open
          variant="page"
          onClose={vi.fn()}
          startRequest={{
            requestKey: 91,
            context_type: 'application',
            context_ref: '42',
            context_label: '去哪儿旅行 · Agent 开发',
            mode: 'nego_coach',
            attachments: [{ kind: 'offer', id: '17', label: '去哪儿旅行 · Agent 开发' }],
            composerDraft: '我想继续讨论这份 Offer 的谈薪策略。',
          }}
          onStartRequestConsumed={claim}
        />
      </AntApp>,
    ));

    expect(claim).toHaveBeenCalledWith(91);
    expect(container.querySelector<HTMLTextAreaElement>('[data-testid="mock-pilot-composer"]')?.value)
      .toBe('我想继续讨论这份 Offer 的谈薪策略。');
    expect(chatState.streamChat).not.toHaveBeenCalled();
  });

  it('sends the clicked Offer attachment instead of an ambient Offer from the same application', async () => {
    chatState.attachments = [
      { kind: 'offer', id: '18', label: '同投递下的另一份 Offer' },
      { kind: 'resume', id: '6', label: '主简历' },
    ];
    chatState.streamChat.mockResolvedValue({
      type: 'message',
      conversation_id: 501,
      message: '谈薪建议',
    });
    const claim = vi.fn(() => true);
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(
      <AntApp>
        <ChatPanel
          open
          offerId={17}
          variant="page"
          onClose={vi.fn()}
          startRequest={{
            requestKey: 93,
            context_type: 'application',
            context_ref: '42',
            context_label: '去哪儿旅行 · Agent 开发',
            mode: 'nego_coach',
            attachments: [{ kind: 'offer', id: '17', label: '去哪儿旅行 · Agent 开发' }],
            composerDraft: '请结合这份 Offer 帮我谈薪。',
          }}
          onStartRequestConsumed={claim}
        />
      </AntApp>,
    ));

    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="mock-pilot-send"]')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(chatState.streamChat).toHaveBeenCalledTimes(1);
    expect(chatState.streamChat.mock.calls[0]?.[2]).toMatchObject({
      context_type: 'application',
      context_ref: '42',
      mode: 'nego_coach',
      attachments: [
        { kind: 'offer', id: '17', label: '去哪儿旅行 · Agent 开发' },
        { kind: 'resume', id: '6', label: '主简历' },
      ],
    });
  });

  it('drops an Offer-scoped draft on a general-chat reopen but preserves the same Offer scope', async () => {
    const request = {
      requestKey: 94,
      context_type: 'application' as const,
      context_ref: '42',
      context_label: '去哪儿旅行 · Agent 开发',
      mode: 'nego_coach' as const,
      attachments: [{ kind: 'offer' as const, id: '17', label: '去哪儿旅行 · Agent 开发' }],
      composerDraft: '只属于 Offer #17 的谈薪草稿',
    };
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(
      <AntApp>
        <ChatPanel open offerId={17} variant="page" onClose={vi.fn()} startRequest={request} />
      </AntApp>,
    ));

    await act(async () => root?.render(
      <AntApp>
        <ChatPanel open={false} variant="page" onClose={vi.fn()} startRequest={request} />
      </AntApp>,
    ));
    await act(async () => root?.render(
      <AntApp>
        <ChatPanel open offerId={17} variant="page" onClose={vi.fn()} startRequest={request} />
      </AntApp>,
    ));
    expect(container.querySelector<HTMLTextAreaElement>('[data-testid="mock-pilot-composer"]')?.value)
      .toBe('只属于 Offer #17 的谈薪草稿');

    await act(async () => root?.render(
      <AntApp>
        <ChatPanel open variant="page" onClose={vi.fn()} startRequest={request} />
      </AntApp>,
    ));
    expect(container.querySelector<HTMLTextAreaElement>('[data-testid="mock-pilot-composer"]')?.value)
      .toBe('');
    expect(chatState.streamChat).not.toHaveBeenCalled();
  });

  it('leaves the current draft untouched when another owner already claimed a start request', async () => {
    const claim = vi.fn((requestKey: number) => requestKey === 91);
    const acceptedRequest = {
      requestKey: 91,
      context_type: 'application' as const,
      context_ref: '42',
      context_label: '去哪儿旅行 · Agent 开发',
      mode: 'nego_coach' as const,
      composerDraft: '保留当前谈薪草稿',
    };
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(
      <AntApp>
        <ChatPanel
          open
          variant="page"
          onClose={vi.fn()}
          startRequest={acceptedRequest}
          onStartRequestConsumed={claim}
        />
      </AntApp>,
    ));

    await act(async () => root?.render(
      <AntApp>
        <ChatPanel
          open
          variant="page"
          onClose={vi.fn()}
          startRequest={{ ...acceptedRequest, requestKey: 92, composerDraft: '不应覆盖当前草稿' }}
          onStartRequestConsumed={claim}
        />
      </AntApp>,
    ));

    expect(claim).toHaveBeenLastCalledWith(92);
    expect(container.querySelector<HTMLTextAreaElement>('[data-testid="mock-pilot-composer"]')?.value)
      .toBe('保留当前谈薪草稿');
    expect(chatState.streamChat).not.toHaveBeenCalled();
  });

  it('keeps an ordinary reply running after close and reports the exact background conversation', async () => {
    let resolveReply: ((value: { type: 'message'; conversation_id: number; message: string }) => void) | undefined;
    chatState.streamChat.mockImplementation(() => new Promise((resolve) => { resolveReply = resolve; }));
    const onReplyLifecycle = vi.fn();
    const startRequest = {
      requestKey: 81,
      context_type: 'application' as const,
      context_ref: '7',
      context_label: '示例公司 · 后端工程师',
      mode: 'general' as const,
      initialMessage: '请总结下一步',
    };
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(
      <AntApp>
        <ChatPanel open variant="drawer" onClose={vi.fn()} startRequest={startRequest} onReplyLifecycle={onReplyLifecycle} />
      </AntApp>,
    ));
    expect(chatState.streamChat).toHaveBeenCalledTimes(1);
    const signal = chatState.streamChat.mock.calls[0][3].signal as AbortSignal;

    await act(async () => root?.render(
      <AntApp>
        <ChatPanel open={false} variant="drawer" onClose={vi.fn()} startRequest={startRequest} onReplyLifecycle={onReplyLifecycle} />
      </AntApp>,
    ));
    expect(signal.aborted).toBe(false);

    await act(async () => {
      resolveReply?.({ type: 'message', conversation_id: 418, message: '这里是整理后的下一步。' });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onReplyLifecycle).toHaveBeenCalledWith({
      status: 'success',
      conversationId: 418,
      background: true,
    });
  });

  it('selects an exact conversation when the mascot notification is opened', async () => {
    const onConversationRequestConsumed = vi.fn();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(
      <AntApp>
        <ChatPanel
          open
          variant="drawer"
          onClose={vi.fn()}
          conversationRequest={{ requestKey: 3, conversationId: 418 }}
          onConversationRequestConsumed={onConversationRequestConsumed}
        />
      </AntApp>,
    ));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(chatState.getConversation).toHaveBeenCalledWith(418);
    expect(onConversationRequestConsumed).toHaveBeenCalledWith(3);
  });

  it('starts the Chat stream with the application context and public pilot action', async () => {
    chatState.streamChat.mockResolvedValue({
      type: 'message',
      conversation_id: 101,
      message: '已准备确认卡',
    });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root?.render(
      <AntApp>
        <ChatPanel
          open
          variant="page"
          onClose={vi.fn()}
          startRequest={{
            requestKey: 1,
            context_type: 'application',
            context_ref: '7',
            context_label: '示例公司 · 后端工程师',
            mode: 'general',
            initialMessage: '保存岗位资料',
            pilot_action: { type: 'application_jd_save' },
          }}
        />
      </AntApp>,
    ));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(chatState.streamChat).toHaveBeenCalledWith(
      '保存岗位资料',
      undefined,
      expect.objectContaining({
        context_type: 'application',
        context_ref: '7',
        pilot_action: { type: 'application_jd_save' },
      }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('does not replay a consumed quick entry after the panel remounts', async () => {
    chatState.streamChat.mockResolvedValue({
      type: 'message',
      conversation_id: 102,
      message: '已准备确认卡',
    });
    const consumed = new Set<number>();
    const claim = (requestKey: number) => {
      if (consumed.has(requestKey)) return false;
      consumed.add(requestKey);
      return true;
    };
    const startRequest = {
      requestKey: 2,
      context_type: 'application' as const,
      context_ref: '7',
      context_label: '示例公司 · 后端工程师',
      mode: 'general' as const,
      initialMessage: '保存岗位资料',
      pilot_action: { type: 'application_jd_save' as const },
    };
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root?.render(
      <AntApp>
        <ChatPanel open variant="page" onClose={vi.fn()} startRequest={startRequest} onStartRequestConsumed={claim} />
      </AntApp>,
    ));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => root?.unmount());
    root = createRoot(container);
    act(() => root?.render(
      <AntApp>
        <ChatPanel open variant="page" onClose={vi.fn()} startRequest={startRequest} onStartRequestConsumed={claim} />
      </AntApp>,
    ));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(chatState.streamChat).toHaveBeenCalledTimes(1);
  });

  it('mounts the application facts and keeps source URL text-only', () => {
    const view = renderProposal(jdAction);
    expect(view.textContent).toContain('示例公司');
    expect(view.textContent).toContain('后端工程师');
    expect(view.textContent).toContain('当前版本');
    expect(view.textContent).toContain('拟创建版本');
    expect(view.textContent).toContain('v4');
    expect(view.textContent).toContain('JD 预览');
    expect(view.textContent).toContain('字符数');
    expect(view.textContent).toContain('Pilot');
    expect(view.textContent).toContain('不会访问链接');
    expect(view.querySelector('a')).toBeNull();
  });

  it('only exposes JD text and source URL as editable controls', () => {
    const view = renderProposal(jdAction);
    const disclosure = view.querySelector<HTMLButtonElement>('[aria-expanded]');
    act(() => disclosure?.click());
    expect(view.querySelector('textarea')).not.toBeNull();
    expect(view.querySelector('input')).not.toBeNull();
    expect(view.textContent).not.toContain('application_id');
    expect(view.textContent).not.toContain('idempotency_key');
  });

  it('renders a full-width frozen submission confirmation without a thin-evidence warning', () => {
    const view = renderProposal({
      tool_name: 'create_application_submission_snapshot',
      human: '请确认冻结这次实际投递使用的简历、岗位资料和材料。',
      confirmation_token: 'token-snapshot',
      args: {
        application_id: 7,
        resume_id: 2,
        jd_version_id: 3,
        material_kit_id: 5,
        submitted_at: '2026-08-12T09:30:00Z',
        note: '官网投递，附作品集。',
        idempotency_key: 'snapshot-key-0001',
      },
    });

    expect(view.textContent).toContain('冻结投递事实');
    expect(view.textContent).toContain('简历 #2');
    expect(view.textContent).toContain('JD 版本 #3');
    expect(view.textContent).toContain('材料包 #5');
    expect(view.textContent).toContain('确认冻结投递事实');
    expect(view.textContent).not.toContain('参考依据较少');
    expect(view.textContent).not.toContain('idempotency_key');
  });

  it('keeps external feedback, personal reflection and next action visibly separated', () => {
    const view = renderProposal({
      tool_name: 'record_application_outcome',
      human: '请确认记录这次投递进展、原始反馈和下一步行动。',
      confirmation_token: 'token-outcome',
      args: {
        application_id: 7,
        submission_snapshot_id: 11,
        application_event_id: 9,
        stage: 'interview',
        result: 'advanced',
        feedback_text: '面试官反馈：项目讲解清楚，系统设计还可深入。',
        reflection_text: '我在容量估算时缺少量级依据。',
        next_action_text: '完成一轮容量估算专项练习。',
        feedback_tags: ['communication', 'system_design'],
        occurred_at: '2026-08-12T11:00:00Z',
        idempotency_key: 'outcome-key-00001',
      },
    });

    expect(view.textContent).toContain('记录投递结果');
    expect(view.textContent).toContain('面试');
    expect(view.textContent).toContain('进入下一阶段');
    expect(view.textContent).toContain('沟通表达 · 系统设计');
    expect(view.textContent).toContain('原始反馈');
    expect(view.textContent).toContain('我的复盘');
    expect(view.textContent).toContain('下次行动');
    expect(view.textContent).toContain('确认记录投递结果');
    expect(view.textContent).toContain('不会生成录用概率或能力评分');
  });
});
