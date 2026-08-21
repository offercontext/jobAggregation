// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Application } from '@/types/application';
import OfferCenterView from './OfferCenterView';
import ResumeLibraryView from './ResumeLibraryView';
import CalendarView from './CalendarView';

const queryState = vi.hoisted(() => ({
  current: {
    data: [] as unknown,
    isLoading: false,
    isError: false,
    isFetching: false,
    refetch: vi.fn(),
  },
}));

const antdState = vi.hoisted(() => ({
  message: {
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
  },
}));

vi.mock('@tanstack/react-query', () => ({
  useMutation: () => ({ isPending: false, mutate: vi.fn() }),
  useQuery: () => queryState.current,
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock('antd', () => {
  const Button = (props: any) => (
    <button type="button" disabled={props.disabled} onClick={props.onClick}>{props.children}</button>
  );
  const Input = Object.assign(
    (props: any) => <input value={props.value} onChange={props.onChange} />,
    { Search: (props: any) => <input value={props.value} onChange={props.onChange} /> },
  );
  const Empty = (props: any) => <div>{props.description}{props.children}</div>;
  const Spin = () => <div>loading-spinner</div>;
  const Box = (props: any) => <div>{props.children}</div>;
  const Typography = {
    Text: (props: any) => <span>{props.children}</span>,
    Title: (props: any) => <h2>{props.children}</h2>,
  };

  return {
    Alert: (props: any) => <div>{props.message}{props.description}</div>,
    Button,
    Col: Box,
    Dropdown: Box,
    Empty,
    Input,
    message: antdState.message,
    Popconfirm: Box,
    Row: Box,
    Space: Box,
    Spin,
    Statistic: (props: any) => <div>{props.title}{props.value}</div>,
    Tag: Box,
    Tooltip: Box,
    Typography,
  };
});

vi.mock('@ant-design/icons', () => ({
  CloudUploadOutlined: () => null,
  DeleteOutlined: () => null,
  EditOutlined: () => null,
  FileAddOutlined: () => null,
  FileTextOutlined: () => null,
  LeftOutlined: () => null,
  MoreOutlined: () => null,
  PlusOutlined: () => null,
  RightOutlined: () => null,
  SwapOutlined: () => null,
}));

vi.mock('@/components/OfferCard', () => ({ default: () => <div /> }));
vi.mock('@/components/AddOfferForm', () => ({ default: () => <div /> }));
vi.mock('@/components/OfferCompareDrawer', () => ({ default: () => <div /> }));
vi.mock('@/components/ResumeCard', () => ({ default: () => <div /> }));
vi.mock('@/components/ResumeUploadModal', () => ({ default: () => <div /> }));
vi.mock('@/components/ResumeEditorDrawer', () => ({ default: () => <div /> }));
vi.mock('@/components/ScheduleEventForm', () => ({ default: () => <div /> }));

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const application = { id: 1 } as Application;
const focusEvent = { kind: 'event' as const, id: 9, scheduledAt: '2026-07-11T09:30:00Z' };

let container: HTMLDivElement | undefined;
let root: Root | undefined;

function render(ui: React.ReactNode) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(ui));
  return container;
}

function rerender(ui: React.ReactNode) {
  act(() => root?.render(ui));
}

function setQueryState({
  data = [],
  isLoading = false,
  isError = false,
  isFetching = false,
}: Partial<typeof queryState.current>) {
  queryState.current = {
    data,
    isLoading,
    isError,
    isFetching,
    refetch: vi.fn(),
  };
}

beforeEach(() => {
  window.scrollTo = vi.fn();
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = undefined;
  container = undefined;
  vi.clearAllMocks();
});

describe('evidence destination query states', () => {
  it('renders explicit Offer loading instead of an empty state', () => {
    setQueryState({ isLoading: true });

    const view = render(<OfferCenterView applications={[application]} onCoach={vi.fn()} />);

    expect(view.textContent).toContain('正在加载 Offer');
  });

  it('renders explicit Resume loading instead of an empty library', () => {
    setQueryState({ isLoading: true });

    const view = render(<ResumeLibraryView />);

    expect(view.textContent).toContain('正在加载简历');
  });

  it('keeps Resume hooks stable when loading resolves to a successful library page', () => {
    setQueryState({ isLoading: true });
    const view = render(<ResumeLibraryView />);

    setQueryState({ data: [] });
    expect(() => rerender(<ResumeLibraryView />)).not.toThrow();
    expect(view.textContent).toContain('简历库');
  });

  it('prioritises Calendar selected-date loading over empty-day copy', () => {
    setQueryState({ isLoading: true });

    const view = render(
      <CalendarView applications={[application]} onOpenDetail={vi.fn()} focusEvent={focusEvent} />,
    );

    expect(view.textContent).toContain('正在加载日程');
    expect(view.textContent).not.toContain('这一天没有记录');
  });

  it('keeps Offer focus on query error and retries without a missing-record warning', () => {
    setQueryState({ isError: true });
    const consumed = vi.fn();

    const view = render(
      <OfferCenterView
        applications={[application]}
        onCoach={vi.fn()}
        focusOfferId={9}
        onEvidenceFocusConsumed={consumed}
      />,
    );

    expect(view.textContent).toContain('加载 Offer 失败');
    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(consumed).not.toHaveBeenCalled();
    act(() => view.querySelector('button')?.click());
    expect(queryState.current.refetch).toHaveBeenCalledTimes(1);
  });

  it('keeps Resume focus on query error and retries without a missing-record warning', () => {
    setQueryState({ isError: true });
    const consumed = vi.fn();

    const view = render(<ResumeLibraryView focusResumeId={9} onEvidenceFocusConsumed={consumed} />);

    expect(view.textContent).toContain('加载简历失败');
    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(consumed).not.toHaveBeenCalled();
    act(() => view.querySelector('button')?.click());
    expect(queryState.current.refetch).toHaveBeenCalledTimes(1);
  });

  it('keeps Calendar focus on query error and retries without a missing-record warning', () => {
    setQueryState({ isError: true });
    const consumed = vi.fn();

    const view = render(
      <CalendarView
        applications={[application]}
        onOpenDetail={vi.fn()}
        focusEvent={focusEvent}
        onEvidenceFocusConsumed={consumed}
      />,
    );

    expect(view.textContent).toContain('加载日程失败');
    expect(view.textContent).not.toContain('这一天没有记录');
    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(consumed).not.toHaveBeenCalled();
    act(() => Array.from(view.querySelectorAll('button')).find((button) => button.textContent === '重试')?.click());
    expect(queryState.current.refetch).toHaveBeenCalledTimes(1);
  });

  it('waits for an Offer background refetch before resolving focus', () => {
    const consumed = vi.fn();
    setQueryState({ data: [], isFetching: true });
    render(
      <OfferCenterView
        applications={[application]}
        onCoach={vi.fn()}
        focusOfferId={9}
        onEvidenceFocusConsumed={consumed}
      />,
    );

    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(consumed).not.toHaveBeenCalled();

    setQueryState({ data: [{ id: 9, signing_bonus: 0, total_cash: 0 }], isFetching: false });
    rerender(<OfferCenterView applications={[application]} onCoach={vi.fn()} focusOfferId={9} onEvidenceFocusConsumed={consumed} />);

    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(consumed).toHaveBeenCalledTimes(1);
  });

  it('waits for a Resume background refetch before resolving focus', () => {
    const consumed = vi.fn();
    setQueryState({ data: [], isFetching: true });
    render(<ResumeLibraryView focusResumeId={9} onEvidenceFocusConsumed={consumed} />);

    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(consumed).not.toHaveBeenCalled();

    setQueryState({ data: [{ id: 9 }], isFetching: false });
    rerender(<ResumeLibraryView focusResumeId={9} onEvidenceFocusConsumed={consumed} />);

    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(consumed).toHaveBeenCalledTimes(1);
  });

  it('waits for a Calendar background refetch before resolving focus', () => {
    const consumed = vi.fn();
    setQueryState({ data: [], isFetching: true });
    const view = render(
      <CalendarView
        applications={[application]}
        onOpenDetail={vi.fn()}
        focusEvent={focusEvent}
        onEvidenceFocusConsumed={consumed}
      />,
    );

    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(consumed).not.toHaveBeenCalled();

    setQueryState({
      data: [{ app_id: 1, date: '2026-07-11', event_id: 9, title: 'Interview', type: 'interview' }],
      isFetching: false,
    });
    rerender(
      <CalendarView
        applications={[application]}
        onOpenDetail={vi.fn()}
        focusEvent={focusEvent}
        onEvidenceFocusConsumed={consumed}
      />,
    );

    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(consumed).toHaveBeenCalledTimes(1);
    expect(view.querySelector('[class*="entryItemFocused"]')).not.toBeNull();
  });

  it('clears focused Calendar evidence before manually opening another populated day', () => {
    setQueryState({
      data: [
        { app_id: 1, date: '2026-07-11', event_id: 9, title: 'Focused', type: 'interview' },
        { app_id: 1, date: '2026-07-12', event_id: 10, title: 'Other', type: 'interview' },
      ],
    });
    const view = render(
      <CalendarView applications={[application]} onOpenDetail={vi.fn()} focusEvent={focusEvent} onEvidenceFocusConsumed={vi.fn()} />,
    );

    expect(view.querySelector('[class*="entryItemFocused"]')).not.toBeNull();
    act(() => Array.from(view.querySelectorAll('button')).find((button) => button.textContent === '返回日历')?.click());
    const otherDay = Array.from(view.querySelectorAll('div')).find((element) => element.textContent === '12');
    act(() => otherDay?.parentElement?.click());

    expect(antdState.message.warning).not.toHaveBeenCalled();
    expect(view.querySelector('[class*="entryItemFocused"]')).toBeNull();
  });
});
