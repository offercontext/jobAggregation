// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  exportBackup: vi.fn(),
  getLogs: vi.fn(),
  getSettings: vi.fn(),
  getSettingsBackup: vi.fn(),
  invalidateQueries: vi.fn(),
  keepPreviousData: Symbol('keepPreviousData'),
  refetchLogs: vi.fn(),
  useQuery: vi.fn(),
  useQueryClient: vi.fn(),
  useMutation: vi.fn(),
}));

vi.mock('@tanstack/react-query', () => ({
  keepPreviousData: mocks.keepPreviousData,
  useMutation: mocks.useMutation,
  useQuery: mocks.useQuery,
  useQueryClient: mocks.useQueryClient,
}));

vi.mock('@/services/chat', () => ({
  exportBackup: mocks.exportBackup,
  getLogs: mocks.getLogs,
  getSettings: mocks.getSettings,
  getSettingsBackup: mocks.getSettingsBackup,
}));

const { default: SettingsView } = await import('./SettingsView');

type LogsQueryConfig = {
  placeholderData?: unknown;
  queryFn: () => Promise<unknown>;
  queryKey: readonly unknown[];
  refetchInterval?: boolean | number;
};

const logPage = {
  entries: [{ level: 'INFO', message: 'page entry' }],
  total: 40,
  limit: 20,
  offset: 0,
  has_more: true,
};

let container: HTMLDivElement;
let root: Root;
let logsResult: Record<string, unknown>;

function latestLogsQuery(): LogsQueryConfig {
  const query = [...mocks.useQuery.mock.calls]
    .map(([config]) => config as LogsQueryConfig)
    .reverse()
    .find((config) => config.queryKey[0] === 'runtime-logs');

  if (!query) throw new Error('runtime logs query was not configured');
  return query;
}

async function renderSettings() {
  await act(async () => {
    root.render(
      <SettingsView
        pilotMascotVisible
        onPilotMascotVisibleChange={vi.fn()}
      />,
    );
  });
}

async function click(element: Element) {
  await act(async () => {
    element.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockImplementation((media: string) => ({
      addEventListener: vi.fn(),
      addListener: vi.fn(),
      dispatchEvent: vi.fn(),
      matches: false,
      media,
      onchange: null,
      removeEventListener: vi.fn(),
      removeListener: vi.fn(),
    })),
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  logsResult = {
    data: logPage,
    isError: false,
    isFetching: false,
    isLoading: false,
    refetch: mocks.refetchLogs,
  };
  mocks.useQuery.mockImplementation((config: { queryKey: readonly unknown[] }) =>
    config.queryKey[0] === 'settings-summary' ? { data: undefined } : logsResult
  );
  mocks.useQueryClient.mockReturnValue({ invalidateQueries: mocks.invalidateQueries });
  mocks.useMutation.mockReturnValue({ isPending: false, mutate: vi.fn() });
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  vi.clearAllMocks();
});

describe('SettingsView runtime diagnostics pagination', () => {
  it('queries page two at offset 20 without polling', async () => {
    await renderSettings();

    const pageTwo = container.querySelector('[title="2"]');
    expect(pageTwo).not.toBeNull();

    await click(pageTwo!);

    const query = latestLogsQuery();
    expect(query.queryKey).toEqual(['runtime-logs', 20, 20, '']);
    expect(query.refetchInterval).toBe(false);
    await query.queryFn();
    expect(mocks.getLogs).toHaveBeenCalledWith(20, 20, '');
  });

  it('uses a 360px accessible diagnostics viewport on the first page', async () => {
    await renderSettings();

    const viewport = container.querySelector('[aria-label="运行日志列表"]') as HTMLElement | null;
    expect(viewport).not.toBeNull();
    expect(viewport?.style.height).toBe('360px');
    expect(viewport?.style.overflowY).toBe('auto');
    expect(viewport?.style.overscrollBehavior).toBe('contain');
  });

  it('shows a retryable failure instead of the empty state before any page loads', async () => {
    logsResult = {
      data: undefined,
      isError: true,
      isFetching: false,
      isLoading: false,
      refetch: mocks.refetchLogs,
    };

    await renderSettings();

    expect(container.textContent).toContain('日志加载失败');
    expect(container.textContent).not.toContain('暂无日志');
    const retry = container.querySelector('[aria-label="重试日志加载"]');
    expect(retry).not.toBeNull();
    await click(retry!);
    expect(mocks.refetchLogs).toHaveBeenCalledTimes(1);
  });

  it('resets to page one and invalidates only the newest page when refreshed from page two', async () => {
    await renderSettings();

    const pageTwo = container.querySelector('[title="2"]');
    expect(pageTwo).not.toBeNull();
    await click(pageTwo!);

    const refresh = container.querySelector('[aria-label="刷新日志"]');
    expect(refresh).not.toBeNull();
    await click(refresh!);

    expect(mocks.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['runtime-logs', 20, 0, ''],
      exact: true,
    });
    expect(latestLogsQuery().queryKey).toEqual(['runtime-logs', 20, 0, '']);
  });

  it('polls the newest page every 15 seconds', async () => {
    await renderSettings();

    const query = latestLogsQuery();
    expect(query.queryKey).toEqual(['runtime-logs', 20, 0, '']);
    expect(query.refetchInterval).toBe(15000);
    expect(query.placeholderData).toBe(mocks.keepPreviousData);
  });

  it('keeps a previous page visible with a retryable refresh warning', async () => {
    logsResult = {
      data: logPage,
      isError: true,
      isFetching: false,
      isLoading: false,
      refetch: mocks.refetchLogs,
    };

    await renderSettings();

    expect(container.textContent).toContain('日志刷新失败，正在显示上一页结果');
    const retry = container.querySelector('[aria-label="重试日志加载"]');
    expect(retry).not.toBeNull();
    await click(retry!);
    expect(mocks.refetchLogs).toHaveBeenCalledTimes(1);
  });

  it('keeps the last successful page visible when a newly selected page fails', async () => {
    await renderSettings();
    logsResult = {
      data: undefined,
      isError: true,
      isFetching: false,
      isLoading: false,
      refetch: mocks.refetchLogs,
    };

    const pageTwo = container.querySelector('[title="2"]');
    expect(pageTwo).not.toBeNull();
    await click(pageTwo!);

    expect(container.textContent).toContain('page entry');
    expect(container.textContent).toContain('日志刷新失败，正在显示上一页结果');
    expect(container.textContent).not.toContain('日志加载失败');
    const retry = container.querySelector('[aria-label="重试日志加载"]');
    expect(retry).not.toBeNull();
    await click(retry!);
    expect(mocks.refetchLogs).toHaveBeenCalledTimes(1);
  });
});
