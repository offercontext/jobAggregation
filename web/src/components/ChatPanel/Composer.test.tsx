// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Composer from './Composer';

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement | undefined;
let root: Root | undefined;

function render(ui: React.ReactNode) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(ui));
  return container;
}

function changeValue(input: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = undefined;
  container = undefined;
});

describe('Composer quick questions', () => {
  it('uses the shared controlled draft without sending it', () => {
    const onSend = vi.fn();
    const onDraftChange = vi.fn();
    const view = render(
      <Composer
        capabilities={[]}
        draftValue="和 Pilot 讨论这份 Offer"
        onDraftChange={onDraftChange}
        onSend={onSend}
      />,
    );
    const input = view.querySelector<HTMLTextAreaElement>('textarea');

    expect(input?.value).toBe('和 Pilot 讨论这份 Offer');
    expect(onSend).not.toHaveBeenCalled();
    act(() => changeValue(input!, '保留新的共享草稿'));
    expect(onDraftChange).toHaveBeenCalledWith('保留新的共享草稿');
  });

  it('fills the composer without sending when a quick question is selected', () => {
    const onSend = vi.fn();
    const view = render(
      <Composer
        capabilities={[]}
        suggestions={['Compare this offer with my application']}
        onSend={onSend}
      />,
    );

    const question = view.querySelector<HTMLButtonElement>('[data-testid="quick-question-0"]');
    expect(question?.textContent).toContain('Compare this offer');

    act(() => question?.click());

    expect(view.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe(
      'Compare this offer with my application',
    );
    expect(onSend).not.toHaveBeenCalled();
  });

  it('hides quick questions after typing and while disabled', () => {
    const view = render(
      <Composer capabilities={[]} suggestions={['Question one']} onSend={vi.fn()} />,
    );
    const input = view.querySelector<HTMLTextAreaElement>('textarea');

    expect(view.querySelector('[data-testid="quick-question-0"]')).not.toBeNull();
    act(() => changeValue(input!, 'A draft question'));
    expect(view.querySelector('[data-testid="quick-question-0"]')).toBeNull();

    act(() => root?.render(<Composer capabilities={[]} suggestions={['Question one']} disabled onSend={vi.fn()} />));
    expect(view.querySelector('[data-testid="quick-question-0"]')).toBeNull();
  });
});

describe('Composer onboarding focus', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('focuses the local textarea without changing a draft or sending a message', () => {
    const onSend = vi.fn();
    const view = render(<Composer capabilities={[]} onboardingFocusToken={1} onSend={onSend} />);
    const input = view.querySelector<HTMLTextAreaElement>('textarea');

    act(() => vi.advanceTimersByTime(1));
    expect(document.activeElement).toBe(input);

    act(() => changeValue(input!, 'Keep this draft'));
    expect(input?.value).toBe('Keep this draft');
    expect(onSend).not.toHaveBeenCalled();
    expect(view.firstElementChild?.className).toContain('composerOnboardingFocus');

    act(() => vi.advanceTimersByTime(2399));
    expect(view.firstElementChild?.className).not.toContain('composerOnboardingFocus');
  });

  it('does not replay a consumed token after remounting and accepts a newer token', () => {
    const onSend = vi.fn();
    const firstView = render(<Composer capabilities={[]} onboardingFocusToken={1} onSend={onSend} />);
    const firstInput = firstView.querySelector<HTMLTextAreaElement>('textarea');

    act(() => vi.advanceTimersByTime(1));
    expect(document.activeElement).toBe(firstInput);

    act(() => root?.render(<Composer capabilities={[]} onboardingFocusToken={0} onSend={onSend} />));
    act(() => root?.unmount());
    container?.remove();
    root = undefined;
    container = undefined;

    const remountedView = render(<Composer capabilities={[]} onboardingFocusToken={0} onSend={onSend} />);
    const remountedInput = remountedView.querySelector<HTMLTextAreaElement>('textarea');
    act(() => vi.advanceTimersByTime(1));
    expect(document.activeElement).not.toBe(remountedInput);

    act(() => root?.render(<Composer capabilities={[]} onboardingFocusToken={2} onSend={onSend} />));
    act(() => vi.advanceTimersByTime(1));
    expect(document.activeElement).toBe(remountedInput);
    expect(onSend).not.toHaveBeenCalled();
  });
});
