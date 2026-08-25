// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MaterialRevisionProposal } from '@/types/materialRevisionProposal';

const proposalService = vi.hoisted(() => ({
  acceptMaterialRevisionProposal: vi.fn(),
  rejectMaterialRevisionProposal: vi.fn(),
}));

vi.mock('@/services/materialRevisionProposals', () => proposalService);
vi.mock('antd', () => ({
  Alert: ({ message }: any) => <div role="alert">{message}</div>,
  Button: ({ children, ...props }: any) => <button type="button" {...props}>{children}</button>,
  Checkbox: ({ checked, onChange, ...props }: any) => (
    <input type="checkbox" checked={checked} onChange={(event) => onChange?.({ target: { checked: event.target.checked } })} {...props} />
  ),
  Modal: ({ open, title, children, footer, onOk, okText, cancelText }: any) => open ? (
    <section role="dialog" aria-label={title}>
      <h2>{title}</h2>
      {children}
      {footer}
      {onOk ? <button type="button" onClick={onOk}>{okText}</button> : null}
      {onOk ? <button type="button">{cancelText}</button> : null}
    </section>
  ) : null,
  Space: ({ children }: any) => <div>{children}</div>,
  Tag: ({ children }: any) => <span>{children}</span>,
  Typography: {
    Paragraph: ({ children }: any) => <p>{children}</p>,
    Text: ({ children }: any) => <span>{children}</span>,
  },
}));

const { default: MaterialProposalReviewModal } = await import('./MaterialProposalReviewModal');

const proposal: MaterialRevisionProposal = {
  id: 3,
  application_id: 7,
  material_kit_id: 5,
  source_resume_id: 11,
  status: 'draft',
  summary: 'Tailor backend experience.',
  proposal_sha256: 'abc',
  result_resume_id: null,
  created_at: '2026-07-15T00:00:00Z',
  changes: [{
    id: 'change-fastapi',
    path: '/experience/0/highlights/0',
    before: 'Built APIs',
    after: 'Built FastAPI APIs',
    rationale: 'Make the existing API experience specific.',
    evidence_refs: [{ source: 'resume', path: '/experience/0/highlights/0', excerpt: 'Built APIs' }],
  }],
  source: {
    application: { id: 7, company_name: 'Acme', position_name: 'Backend' },
    material_kit: { id: 5, jd_excerpt: 'FastAPI backend' },
    resume: { id: 11, title: 'Backend Resume' },
    latest_evidence_bundle: null,
    user_assertions: [{ id: 'assertion-1', text: 'I led the migration.' }],
  },
  accepted_change_ids: [],
  accepted_at: null,
  rejected_at: null,
};

let root: Root | undefined;
let container: HTMLDivElement | undefined;

function render(nextProposal: MaterialRevisionProposal = proposal) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(
    <MaterialProposalReviewModal
      applicationID={7}
      proposal={nextProposal}
      open
      onClose={vi.fn()}
      onAccepted={vi.fn()}
    />,
  ));
  return container;
}

function button(view: HTMLDivElement, label: string) {
  return [...view.querySelectorAll('button')].find((item) => item.textContent?.includes(label)) as HTMLButtonElement;
}

beforeEach(() => {
  proposalService.acceptMaterialRevisionProposal.mockReset();
  proposalService.rejectMaterialRevisionProposal.mockReset();
  proposalService.acceptMaterialRevisionProposal.mockResolvedValue({});
  proposalService.rejectMaterialRevisionProposal.mockResolvedValue({});
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = undefined;
  container = undefined;
});

describe('MaterialProposalReviewModal', () => {
  const knownLegacyFixedPhrases = [
    'AI recommendation',
    'Reject proposal',
    'Accept selected changes',
    'Generate evidence-gated resume proposal',
    'User assertion supplied for this proposal',
    'No safe evidence-backed changes are available.',
  ];

  it('shows evidence, user assertion labels, and selects all changes by default', () => {
    const view = render();

    expect(view.textContent).toContain('AI 建议，仅供人工审核');
    expect(view.textContent).toContain('Built APIs');
    expect(view.textContent).toContain('用户断言：I led the migration.');
    expect(view.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(true);
    expect(button(view, '接受选中的修改')?.disabled).toBe(false);
    for (const phrase of knownLegacyFixedPhrases) {
      expect(view.textContent).not.toContain(phrase);
    }
  });

  it('maps every material evidence source to a fixed Chinese label while preserving paths and excerpts', () => {
    const sourceCoverageProposal: MaterialRevisionProposal = {
      ...proposal,
      changes: [{
        ...proposal.changes[0],
        evidence_refs: [
          { source: 'resume', path: '/experience/0/highlights/0', excerpt: 'Built APIs' },
          { source: 'evidence_bundle', path: '/resume/content_json/experience/0/highlights/0', excerpt: 'Built APIs' },
          { source: 'user_assertion', path: '/user_assertions/0/text', excerpt: 'I led the migration.' },
        ],
      }],
    };
    const view = render(sourceCoverageProposal);

    expect(view.textContent).toContain('简历');
    expect(view.textContent).toContain('本次投递记录');
    expect(view.textContent).toContain('用户断言');
    expect(view.textContent).toContain('/resume/content_json/experience/0/highlights/0: Built APIs');
    expect(view.textContent).toContain('/user_assertions/0/text: I led the migration.');
  });

  it('shows a fixed Chinese empty state without rendering the model empty summary', () => {
    const view = render({
      ...proposal,
      summary: 'No safe evidence-backed changes are available.',
      changes: [],
    });

    expect(view.textContent).toContain('当前没有可由证据安全支持的简历改写建议');
    expect(view.textContent).not.toContain('No safe evidence-backed changes are available.');
    expect(button(view, '接受选中的修改')?.disabled).toBe(true);
  });

  it('requires a second confirmation and sends selected ids and proposal hash', async () => {
    const view = render();
    const checkbox = view.querySelector<HTMLInputElement>('input[type="checkbox"]');
    act(() => checkbox?.click());
    expect(button(view, '接受选中的修改')?.disabled).toBe(true);

    act(() => checkbox?.click());
    act(() => button(view, '接受选中的修改')?.click());
    expect(view.textContent).toContain('这会创建一个新的派生简历版本');
    await act(async () => button(view, '创建派生简历')?.click());

    expect(proposalService.acceptMaterialRevisionProposal).toHaveBeenCalledWith(7, 3, {
      expected_proposal_sha256: 'abc',
      selected_change_ids: ['change-fastapi'],
    });
  });

  it('rejects without calling accept', async () => {
    const view = render();
    await act(async () => button(view, '拒绝提案')?.click());

    expect(proposalService.rejectMaterialRevisionProposal).toHaveBeenCalledWith(7, 3);
    expect(proposalService.acceptMaterialRevisionProposal).not.toHaveBeenCalled();
  });

  it('disables stale acceptance after a source conflict', async () => {
    proposalService.acceptMaterialRevisionProposal.mockRejectedValueOnce({
      isAxiosError: true,
      response: { status: 409 },
    });
    const view = render();
    act(() => button(view, '接受选中的修改')?.click());
    await act(async () => button(view, '创建派生简历')?.click());

    expect(view.textContent).toContain('原始来源已发生变化，请重新生成提案后再审核');
    expect(button(view, '接受选中的修改')?.disabled).toBe(true);
  });

  it('uses a fixed Chinese error for unknown failures without exposing raw error text', async () => {
    proposalService.rejectMaterialRevisionProposal.mockRejectedValueOnce(new Error('SECRET_RAW_ERROR'));
    const view = render();

    await act(async () => button(view, '拒绝提案')?.click());

    expect(view.textContent).toContain('操作未完成，请稍后重试');
    expect(view.textContent).not.toContain('SECRET_RAW_ERROR');
  });
});
