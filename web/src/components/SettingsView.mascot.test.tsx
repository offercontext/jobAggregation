// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  useMutation: vi.fn(),
  useQuery: vi.fn(),
  useQueryClient: vi.fn(),
}));

vi.mock('@tanstack/react-query', () => ({
  keepPreviousData: Symbol('keepPreviousData'),
  useMutation: mocks.useMutation,
  useQuery: mocks.useQuery,
  useQueryClient: mocks.useQueryClient,
}));

vi.mock('@/services/chat', () => ({
  exportBackup: vi.fn(),
  getLogs: vi.fn(),
  getSettings: vi.fn(),
  getSettingsBackup: vi.fn(),
}));

const { default: SettingsView } = await import('./SettingsView');

let container: HTMLDivElement;
let root: Root;
const logsResult = {
  data: { entries: [], total: 0 },
  isError: false,
  isFetching: false,
  refetch: vi.fn(),
};

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({
      matches: false,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  mocks.useQueryClient.mockReturnValue({ invalidateQueries: vi.fn(), setQueryData: vi.fn() });
  mocks.useMutation.mockReturnValue({ isPending: false, mutate: vi.fn() });
  mocks.useQuery.mockImplementation(({ queryKey }: { queryKey: readonly string[] }) =>
    queryKey[0] === 'settings-summary'
      ? { data: undefined }
      : logsResult,
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

describe('SettingsView Pilot mascot preference', () => {
  it('exposes a keyboard-accessible restore switch', () => {
    const onChange = vi.fn();
    act(() => {
      root.render(
        <SettingsView
          pilotMascotVisible={false}
          onPilotMascotVisibleChange={onChange}
        />,
      );
    });
    const toggle = container.querySelector<HTMLElement>('[aria-label="显示 Haru"]');
    expect(toggle).not.toBeNull();
    act(() => toggle!.click());
    expect(onChange).toHaveBeenCalledWith(true, expect.anything());
    expect(container.textContent).toContain('隐藏后将恢复默认 Pilot 侧边栏');
  });
});
