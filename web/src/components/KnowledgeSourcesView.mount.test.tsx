// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  confirmed: [] as unknown[],
}));

vi.mock('@/services/knowledge', () => ({
  archiveKnowledgeSource: vi.fn(),
  buildKnowledgeAssetContentUrl: vi.fn(() => '#'),
  buildKnowledgeSourceContentUrl: vi.fn(() => '#'),
  cancelKnowledgeJob: vi.fn(),
  deleteKnowledgeSource: vi.fn(),
  fetchKnowledgeSource: vi.fn(),
  fetchKnowledgeSourceBrief: vi.fn(),
  fetchKnowledgeSourceContent: vi.fn(),
  fetchKnowledgeSourceEvidence: vi.fn(),
  fetchKnowledgeSourceJobs: vi.fn(),
  fetchKnowledgeSources: vi.fn().mockResolvedValue([]),
  fetchConfirmedInterviewKnowledgeNotes: vi.fn(() => Promise.resolve(state.confirmed)),
  pasteKnowledgeSource: vi.fn(),
  rebuildKnowledgeSourceBrief: vi.fn(),
  searchKnowledgeEvidence: vi.fn(),
  unarchiveKnowledgeSource: vi.fn(),
  updateKnowledgeSourceTitle: vi.fn(),
  uploadKnowledgeBundle: vi.fn(),
  uploadKnowledgeSource: vi.fn(),
}));

const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query');
const { App: AntApp } = await import('antd');
const { default: KnowledgeSourcesView } = await import('./KnowledgeSourcesView');

let root: Root | undefined;
let container: HTMLDivElement | undefined;

function renderView() {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  act(() => root?.render(
    <QueryClientProvider client={queryClient}>
      <AntApp><KnowledgeSourcesView /></AntApp>
    </QueryClientProvider>,
  ));
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

beforeEach(() => {
  state.confirmed = [];
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({ matches: false, addListener: () => undefined, removeListener: () => undefined }),
  });
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = undefined;
  container = undefined;
});

describe('KnowledgeSourcesView mounted source states', () => {
  it('keeps the empty knowledge list neutral', async () => {
    renderView();
    await flush();

    expect(container?.textContent).toContain('暂无复盘沉淀');
    expect(container?.textContent).not.toContain('已保留当时版本');
  });

  it('shows frozen and changed state only for loaded confirmed history', async () => {
    state.confirmed = [{
      id: 1,
      title: '复盘片段',
      source_status: 'source_changed',
      content: { blocks: [{ block_id: 'b1', text: '回答', evidence_refs: [] }] },
      evidence: [],
    }];
    renderView();
    await flush();

    expect(container?.textContent).toContain('原资料已更新，本次结果仍使用旧版');
    expect(container?.textContent).toContain('已保留当时版本');
  });
});
