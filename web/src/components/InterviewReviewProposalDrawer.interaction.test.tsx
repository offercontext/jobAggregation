// @vitest-environment jsdom
import { act, useState, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const service = vi.hoisted(() => ({
  create: vi.fn(),
  get: vi.fn(),
  list: vi.fn(),
}));
const readinessOwner = vi.hoisted(() => ({ render: vi.fn() }));

vi.mock('@/services/interviewReviewProposals', () => {
  class InterviewReviewProposalError extends Error {
    readonly code?: string;

    constructor(message: string, code?: string) {
      super(message);
      this.code = code;
    }
  }
  return {
  createInterviewReviewProposal: service.create,
  getInterviewReviewProposal: service.get,
  listInterviewReviewProposals: service.list,
    InterviewReviewProposalError,
  };
});
vi.mock('./InterviewReviewProposalDrawer.module.css', () => ({ default: {} }));
vi.mock('@/features/reviewReadiness/ReviewReadinessNextStep', () => ({
  ReviewReadinessNextStep: (props: { proposal: { id: number }; draft?: unknown; applicationId?: number }) => {
    readinessOwner.render(props);
    return <div data-testid="readiness-owner">proposal-{props.proposal.id}</div>;
  },
}));
vi.mock('antd', () => {
  const Typography = {
    Paragraph: ({ children }: { children: ReactNode }) => <p>{children}</p>,
    Text: ({ children }: { children: ReactNode }) => <span>{children}</span>,
    Title: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
  };
  const List = Object.assign(
    ({ dataSource, renderItem }: { dataSource: unknown[]; renderItem: (item: unknown) => ReactNode }) => (
      <div>{dataSource.map((item, index) => <div key={index}>{renderItem(item)}</div>)}</div>
    ),
    { Item: ({ children }: { children: ReactNode }) => <div>{children}</div> },
  );
  return {
    Button: ({ children, onClick, disabled }: { children: ReactNode; onClick?: () => void; disabled?: boolean }) => (
      <button type="button" disabled={disabled} onClick={onClick}>{children}</button>
    ),
    Card: ({ title, children }: { title?: ReactNode; children: ReactNode }) => <section><h3>{title}</h3>{children}</section>,
    Empty: ({ description }: { description?: ReactNode }) => <div>{description}</div>,
    List,
    Space: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    Spin: () => <span>loading</span>,
    Tag: ({ children }: { children: ReactNode }) => <span>{children}</span>,
    Typography,
  };
});

const { default: InterviewReviewProposalDrawer } = await import('./InterviewReviewProposalDrawer');

const note = {
  id: 7,
  application_id: 3,
  application_event_id: 9,
  company: 'Example',
  position: 'Engineer',
  round: 'technical',
  date: '2026-07-22',
  questions: 'How do you test?',
  self_reflection: 'I clarified the constraint.',
  difficulty_points: 'The tradeoff was difficult.',
  mood: 'focused',
} as never;

let root: Root | undefined;
let container: HTMLDivElement | undefined;

function Harness() {
  const [open, setOpen] = useState(true);
  const [attemptState, setAttemptState] = useState<{ key: string; result_unknown: boolean; event_id: number | null } | null>(null);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>重新打开</button>
      <button type="button" onClick={() => setOpen(false)}>切换页面</button>
      {open && (
        <InterviewReviewProposalDrawer
          open
          note={note}
          eventID={9}
          attemptState={attemptState}
          onAttemptStateChange={setAttemptState}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

beforeEach(() => {
  service.create.mockReset();
  service.get.mockReset();
  service.list.mockResolvedValue([]);
  readinessOwner.render.mockReset();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue('00000000-0000-0000-0000-000000000001');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  vi.restoreAllMocks();
});

describe('InterviewReviewProposalDrawer attempt ownership', () => {
  it('reuses the unknown attempt key after close and reopen', async () => {
    service.create.mockRejectedValueOnce(new Error('network disconnected'));
    service.create.mockResolvedValueOnce({
      id: 20,
      created_at: '2026-07-22T00:00:00Z',
      source_status: 'current',
      proposal: {
        summary: { text: 'safe', evidence_refs: [] },
        observations: [],
        clarifications: [],
        practice_focuses: [],
        next_questions: [],
      },
    });

    act(() => root?.render(<Harness />));
    const generate = () => [...(container?.querySelectorAll('button') || [])]
      .find((button) => button.textContent === '生成复盘建议') as HTMLButtonElement;

    await act(async () => {
      generate().click();
      await Promise.resolve();
    });
    expect(service.create).toHaveBeenLastCalledWith(7, '00000000-0000-0000-0000-000000000001');

    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '关闭')
        ?.click();
    });
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '重新打开')
        ?.click();
    });
    await act(async () => { await Promise.resolve(); });

    await act(async () => {
      generate().click();
      await Promise.resolve();
    });

    expect(service.create).toHaveBeenNthCalledWith(2, 7, '00000000-0000-0000-0000-000000000001');
  });

  it('creates a new key after a successful response and reopening', async () => {
    service.create.mockResolvedValue({
      id: 20,
      created_at: '2026-07-22T00:00:00Z',
      source_status: 'current',
      proposal: {
        summary: { text: 'safe', evidence_refs: [] },
        observations: [],
        clarifications: [],
        practice_focuses: [],
        next_questions: [],
      },
    });
    vi.spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValueOnce('00000000-0000-0000-0000-000000000001')
      .mockReturnValueOnce('00000000-0000-0000-0000-000000000002');

    act(() => root?.render(<Harness />));
    const generate = () => [...(container?.querySelectorAll('button') || [])]
      .find((button) => button.textContent === '生成复盘建议') as HTMLButtonElement;
    await act(async () => {
      generate().click();
      await Promise.resolve();
    });
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '关闭')
        ?.click();
    });
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '重新打开')
        ?.click();
    });
    await act(async () => { await Promise.resolve(); });
    await act(async () => {
      generate().click();
      await Promise.resolve();
    });

    expect(service.create).toHaveBeenNthCalledWith(2, 7, '00000000-0000-0000-0000-000000000002');
  });

  it('retains the attempt key after a stable provider 502', async () => {
    service.create.mockRejectedValueOnce(
      new (await import('@/services/interviewReviewProposals')).InterviewReviewProposalError(
        'provider detail must not be shown',
        'interview_review_provider_error',
      ),
    );

    act(() => root?.render(<Harness />));
    const generate = () => {
      const buttons = [...(container?.querySelectorAll('button') || [])];
      return buttons[buttons.length - 1] as HTMLButtonElement;
    };

    await act(async () => {
      generate().click();
      await Promise.resolve();
    });

    expect(service.create).toHaveBeenCalledWith(7, '00000000-0000-0000-0000-000000000001');
    expect(container?.textContent).not.toContain('provider detail must not be shown');

    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '鍏抽棴')
        ?.click();
    });
    service.create.mockResolvedValueOnce({
      id: 20,
      created_at: '2026-07-22T00:00:00Z',
      source_status: 'current',
      proposal: {
        summary: { text: 'safe', evidence_refs: [] },
        observations: [],
        clarifications: [],
        practice_focuses: [],
        next_questions: [],
      },
    });
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '閲嶆柊鎵撳紑')
        ?.click();
    });
    await act(async () => { await Promise.resolve(); });
    await act(async () => {
      generate().click();
      await Promise.resolve();
    });
    expect(service.create).toHaveBeenNthCalledWith(2, 7, '00000000-0000-0000-0000-000000000001');
  });

  it('keeps the key when the parent unmounts during a pending request', async () => {
    let resolveFirst: ((value: unknown) => void) | undefined;
    service.create.mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve; }));
    service.create.mockResolvedValueOnce({
      id: 20,
      created_at: '2026-07-22T00:00:00Z',
      source_status: 'current',
      proposal: {
        summary: { text: 'safe', evidence_refs: [] },
        observations: [],
        clarifications: [],
        practice_focuses: [],
        next_questions: [],
      },
    });

    act(() => root?.render(<Harness />));
    const generate = () => [...(container?.querySelectorAll('button') || [])]
      .find((button) => button.textContent === '生成复盘建议') as HTMLButtonElement;
    await act(async () => {
      generate().click();
      await Promise.resolve();
    });
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '切换页面')
        ?.click();
    });
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '重新打开')
        ?.click();
    });
    await act(async () => { await Promise.resolve(); });
    await act(async () => {
      generate().click();
      await Promise.resolve();
    });

    expect(service.create).toHaveBeenNthCalledWith(2, 7, '00000000-0000-0000-0000-000000000001');
    act(() => { resolveFirst?.({}); });
  });

  it('renders a safe empty proposal as a normal empty state', async () => {
    service.create.mockResolvedValueOnce({
      id: 20,
      created_at: '2026-07-22T00:00:00Z',
      source_status: 'current',
      proposal: {
        summary: { text: '暂无可验证建议', evidence_refs: [] },
        observations: [],
        clarifications: [],
        practice_focuses: [],
        next_questions: [],
      },
    });

    act(() => root?.render(<Harness />));
    const generate = () => {
      const buttons = [...(container?.querySelectorAll('button') || [])];
      return buttons[buttons.length - 1] as HTMLButtonElement;
    };
    await act(async () => {
      generate().click();
      await Promise.resolve();
    });

    expect(container?.textContent).toContain('暂无可验证建议');
    expect(container?.querySelector('[role="alert"]')).toBeNull();
  });

  it('blocks non-pending history on remount before selecting the pending proposal owner', async () => {
    const item = (id: number) => ({
      id, note_id: 7, application_event_id: 9, proposal_schema_version: 2,
      source_note_revision: 1, source_status: 'current', proposal_hash: `hash-${id}`,
      created_at: `2026-07-${String(id).padStart(2, '0')}T00:00:00Z`,
      proposal: { summary: { text: `proposal ${id}`, evidence_refs: [] }, observations: [], clarifications: [], practice_focuses: [], next_questions: [] },
    });
    service.list.mockResolvedValue([item(20), item(21)]);
    service.get.mockResolvedValue(item(20));
    const pendingDraft = {
      ownerKey: 'review:4:7:20', ownerGeneration: 4, noteId: 7, proposalId: 20,
      applicationId: 3, selectedFocusId: 'focus-1', userNote: '', idempotencyKey: 'key',
      frozenProposalInput: null, proposalUnknown: false,
      actionDraft: {
        ownerKey: 'review:4:7:20:focus-1', operationId: 'op', actionCallId: 'call', actionName: 'save_review_readiness_signal',
        confirmationToken: 'token', allowedDecisions: ['approve', 'modify', 'reject'], status: 'proposed', result: null,
        originalPayload: {}, pendingDecision: null, resultUnknown: false,
      },
    } as never;
    act(() => root?.render(<InterviewReviewProposalDrawer open note={note} eventID={9} ownerGeneration={4} readinessDrafts={{ 'review:4:7:20': pendingDraft }} onClose={() => {}} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    const initialHistoryButtons = [...(container?.querySelectorAll('button') ?? [])].filter((button) => button.textContent?.includes('2026'));
    expect(initialHistoryButtons).toHaveLength(2);
    expect(initialHistoryButtons[0]?.disabled).toBe(false);
    expect(initialHistoryButtons[1]?.disabled).toBe(true);
    initialHistoryButtons[1]?.click();
    expect(service.get).not.toHaveBeenCalled();

    await act(async () => {
      initialHistoryButtons[0]?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    const historyButtons = [...(container?.querySelectorAll('button') ?? [])].filter((button) => button.textContent?.includes('2026'));
    expect(service.get).toHaveBeenCalledTimes(1);
    expect(historyButtons[1]?.disabled).toBe(true);
    const calls = service.get.mock.calls.length;
    historyButtons[1]?.click();
    expect(service.get).toHaveBeenCalledTimes(calls);
  });

  it('does not install a late history response into a newer owner generation', async () => {
    const item = {
      id: 20, note_id: 7, application_event_id: 9, proposal_schema_version: 2,
      source_note_revision: 1, source_status: 'current', proposal_hash: 'hash-20', created_at: '2026-07-20T00:00:00Z',
      proposal: { summary: { text: 'old owner proposal', evidence_refs: [] }, observations: [], clarifications: [], practice_focuses: [], next_questions: [] },
    };
    let resolveHistory!: (value: typeof item) => void;
    service.list.mockResolvedValue([item]);
    service.get.mockReturnValue(new Promise((resolve) => { resolveHistory = resolve; }));
    const render = (generation: number) => root?.render(<InterviewReviewProposalDrawer open note={note} eventID={9} ownerGeneration={generation} onClose={() => {}} />);
    act(() => render(4));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('2026'))?.click());
    act(() => render(5));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => { resolveHistory(item); await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).not.toContain('old owner proposal');
    expect(container?.querySelector('[data-testid="readiness-owner"]')).toBeNull();
  });

  it('does not offer regeneration for changed history while its proposed action still owns rejection', async () => {
    const changed = {
      id: 20, note_id: 7, application_event_id: 9, proposal_schema_version: 2,
      source_note_revision: 1, source_status: 'source_changed', proposal_hash: 'hash-20', created_at: '2026-07-20T00:00:00Z',
      proposal: { summary: { text: 'changed proposal', evidence_refs: [] }, observations: [], clarifications: [], practice_focuses: [], next_questions: [] },
    };
    const pendingDraft = {
      ownerKey: 'review:4:7:20', ownerGeneration: 4, noteId: 7, proposalId: 20,
      applicationId: 3, selectedFocusId: 'focus-1', userNote: '', idempotencyKey: 'key', frozenProposalInput: null, proposalUnknown: false,
      actionDraft: {
        ownerKey: 'review:4:7:20:focus-1', operationId: 'op', actionCallId: 'call', actionName: 'save_review_readiness_signal',
        confirmationToken: 'token', allowedDecisions: ['approve', 'modify', 'reject'], status: 'proposed', result: null,
        originalPayload: {}, pendingDecision: null, resultUnknown: false,
      },
    } as never;
    service.list.mockResolvedValue([changed]);
    service.get.mockResolvedValue(changed);
    act(() => root?.render(<InterviewReviewProposalDrawer open note={note} eventID={9} ownerGeneration={4} readinessDrafts={{ 'review:4:7:20': pendingDraft }} onClose={() => {}} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('2026'))?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(container?.textContent).toContain('来源已变化');
    expect([...container!.querySelectorAll('button')].find((button) => button.textContent === '重新生成复盘建议')).toBeUndefined();
  });

  it('selects an exact application/note/proposal safe terminal across an ordinary generation and hides it from another application owner', async () => {
    const historical = {
      id: 20, note_id: 7, application_event_id: 9, proposal_schema_version: 2 as const,
      source_note_revision: 1, source_fingerprint: 'fingerprint', source_status: 'current' as const,
      proposal_hash: 'hash-20', created_at: '2026-07-20T00:00:00Z',
      proposal: { summary: { text: 'safe terminal proposal', evidence_refs: [] }, observations: [], clarifications: [], practice_focuses: [], next_questions: [] },
    };
    const safeTerminal = {
      ownerKey: 'review:4:7:20', ownerGeneration: 4, noteId: 7, proposalId: 20,
      applicationId: 3, selectedFocusId: null, userNote: '', idempotencyKey: null, frozenProposalInput: null, proposalUnknown: false,
      actionDraft: {
        ownerKey: 'review:4:7:20:terminal', operationId: 'signal-operation', actionCallId: '', actionName: 'save_review_readiness_signal' as const,
        confirmationToken: null, allowedDecisions: [], status: 'committed' as const,
        result: { signal_id: 8, signal_version_id: 9 }, originalPayload: {}, pendingDecision: null, resultUnknown: false,
        undoStatus: null, undoReplayed: false, undoRequest: null, undoResultUnknown: false,
      },
    };
    service.list.mockResolvedValue([historical]);
    service.get.mockResolvedValue(historical);
    const render = (applicationId: number) => root?.render(<InterviewReviewProposalDrawer
      open note={note} applicationId={applicationId} eventID={9} ownerGeneration={5}
      readinessDrafts={{ [safeTerminal.ownerKey]: safeTerminal }} onClose={() => {}}
    />);
    act(() => render(3));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('2026'))?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(readinessOwner.render).toHaveBeenLastCalledWith(expect.objectContaining({
      applicationId: 3,
      draft: expect.objectContaining({ ownerKey: 'review:4:7:20', actionDraft: expect.objectContaining({ operationId: 'signal-operation' }) }),
    }));

    readinessOwner.render.mockClear();
    act(() => render(4));
    expect(readinessOwner.render).toHaveBeenLastCalledWith(expect.objectContaining({ applicationId: 4, draft: null }));
  });
});
