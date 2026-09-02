import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
  type RefObject,
} from 'react';

import {
  requestCoreTaskClose,
  type ActiveCoreTask,
  type CoreTaskCloseGuard,
  type CoreTaskSurfaceController,
} from './controller';
import styles from './CoreTaskSurfaceHost.module.css';

export interface CoreTaskSurfaceHostProps {
  readonly controller: CoreTaskSurfaceController;
  readonly children?: ReactNode;
  readonly owner?: ReactNode;
  readonly renderOwner?: (active: ActiveCoreTask) => ReactNode;
  readonly sourceElement?: HTMLElement | null | (() => HTMLElement | null);
  readonly focusReturnRef?: RefObject<HTMLElement | null>;
  readonly heading?: string;
  readonly className?: string;
  /** Reveal a newly launched owner when it is mounted below the current viewport. */
  readonly revealOnOpen?: boolean;
  /** Synchronously reads the exact active owner's mutation guard. */
  readonly closeGuard?: (active: ActiveCoreTask) => CoreTaskCloseGuard;
}

function resolveElement(
  sourceElement: CoreTaskSurfaceHostProps['sourceElement'],
  focusReturnRef: CoreTaskSurfaceHostProps['focusReturnRef'],
): HTMLElement | null {
  try {
    if (focusReturnRef?.current) return focusReturnRef.current;
    if (typeof sourceElement === 'function') return sourceElement();
    return sourceElement ?? null;
  } catch {
    return null;
  }
}

function readReducedMotion(): boolean {
  try {
    return typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

/** The one display host for a controller-owned task surface. */
export function CoreTaskSurfaceHost({
  controller,
  children,
  owner,
  renderOwner,
  sourceElement,
  focusReturnRef,
  heading = '当前任务',
  className,
  revealOnOpen = false,
  closeGuard,
}: CoreTaskSurfaceHostProps) {
  const state = useSyncExternalStore(controller.subscribe, controller.getState, controller.getState);
  const ownerRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const activeRef = useRef(state.active);
  activeRef.current = state.active;
  const mountedRef = useRef(false);
  const observedGenerationRef = useRef<number | null>(null);
  const [reducedMotion, setReducedMotion] = useState(readReducedMotion);
  const requestClose = (active: ActiveCoreTask) => {
    let guard: CoreTaskCloseGuard = { pending: false, unsaved: false };
    try {
      guard = closeGuard?.(active) ?? guard;
    } catch {
      // A broken guard must fail safe: losing an uncertain operation is worse
      // than retaining an explicitly settleable recovery certificate.
      guard = { pending: true, unsaved: true };
    }
    requestCoreTaskClose(controller, active, guard);
  };

  useEffect(() => controller.subscribeFocus((active) => {
    if (active.generation !== controller.getState().generation) return;
    ownerRef.current?.focus({ preventScroll: true });
  }), [controller]);

  useEffect(() => {
    if (state.active && observedGenerationRef.current !== state.active.generation) {
      observedGenerationRef.current = state.active.generation;
      returnFocusRef.current = resolveElement(sourceElement, focusReturnRef) ?? (
        typeof document === 'undefined' ? null : document.activeElement instanceof HTMLElement ? document.activeElement : null
      );
      ownerRef.current?.focus({ preventScroll: true });
      if (revealOnOpen) {
        ownerRef.current?.scrollIntoView?.({
          behavior: reducedMotion ? 'auto' : 'smooth',
          block: 'start',
        });
      }
    }
    if (state.phase === 'closed' && !state.active) {
      returnFocusRef.current?.focus({ preventScroll: true });
      returnFocusRef.current = null;
      observedGenerationRef.current = null;
    }
  }, [focusReturnRef, reducedMotion, revealOnOpen, sourceElement, state.active, state.phase]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      queueMicrotask(() => {
        if (mountedRef.current || !activeRef.current) return;
        const current = controller.getState().active;
        if (current?.generation === activeRef.current.generation) {
          returnFocusRef.current?.focus({ preventScroll: true });
        }
      });
    };
  }, [controller]);

  useEffect(() => {
    if (state.phase !== 'opening' && state.phase !== 'open' && state.phase !== 'closing') return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (state.active) requestClose(state.active);
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [controller, closeGuard, state.active, state.phase]);

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined;
    let media: MediaQueryList;
    try {
      media = window.matchMedia('(prefers-reduced-motion: reduce)');
    } catch {
      return undefined;
    }
    const onChange = (event?: MediaQueryListEvent) => setReducedMotion(event?.matches ?? media.matches);
    try {
      media.addEventListener?.('change', onChange);
    } catch {
      // Older embedded browsers may only expose addListener.
      try { media.addListener?.(onChange); } catch { /* unavailable */ }
    }
    if (!media.addEventListener && media.addListener) {
      try { media.addListener(onChange); } catch { /* unavailable */ }
    }
    return () => {
      try { media.removeEventListener?.('change', onChange); } catch { /* unavailable */ }
      try { media.removeListener?.(onChange); } catch { /* unavailable */ }
    };
  }, []);

  useEffect(() => {
    if (!reducedMotion || !state.active) return;
    if (state.phase === 'opening') controller.markOpen(state.active.generation);
    else if (state.phase === 'closing') controller.markClosed(state.active.generation);
  }, [controller, reducedMotion, state.active, state.phase]);

  if (!state.active || state.phase === 'closed') return null;
  const active = state.active;
  const content = renderOwner ? renderOwner(active) : (owner ?? children);
  const ownerKey = `${active.key}:${active.generation}`;
  const completeAnimation = (event: React.AnimationEvent<HTMLDivElement> | React.TransitionEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    if (controller.getState().phase === 'opening') controller.markOpen(active.generation);
    else if (controller.getState().phase === 'closing') controller.markClosed(active.generation);
  };

  return (
    <section className={`${styles.surface} ${className ?? ''}`.trim()} aria-labelledby={`core-task-heading-${active.generation}`}>
      <div
        key={ownerKey}
        ref={ownerRef}
        className={`${styles.owner} ${state.phase === 'opening' ? styles.opening : ''} ${state.phase === 'closing' ? styles.closing : ''}`.trim()}
        data-core-task-owner={active.ownerId}
        data-core-task-key={active.key}
        data-core-task-generation={active.generation}
        tabIndex={-1}
        onAnimationEnd={completeAnimation}
        onTransitionEnd={completeAnimation}
      >
        <h2 id={`core-task-heading-${active.generation}`} className={styles.heading}>{heading}</h2>
        <button type="button" className={styles.close} aria-label="关闭任务" onClick={() => requestClose(active)}>
          关闭
        </button>
        <div className={styles.content}>{content}</div>
      </div>
    </section>
  );
}
