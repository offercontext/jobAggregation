import {
  useEffect,
  useRef,
  useSyncExternalStore,
  type ReactNode,
  type RefObject,
} from 'react';

import type { ActiveCoreTask, CoreTaskSurfaceController } from './controller';
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
}: CoreTaskSurfaceHostProps) {
  const state = useSyncExternalStore(controller.subscribe, controller.getState, controller.getState);
  const ownerRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const previousPhaseRef = useRef(state.phase);

  useEffect(() => controller.subscribeFocus((active) => {
    if (active.generation !== controller.getState().generation) return;
    ownerRef.current?.focus({ preventScroll: true });
  }), [controller]);

  useEffect(() => {
    if (state.phase === 'opening' && !returnFocusRef.current) {
      returnFocusRef.current = resolveElement(sourceElement, focusReturnRef) ?? (
        typeof document === 'undefined' ? null : document.activeElement instanceof HTMLElement ? document.activeElement : null
      );
    }
    if (state.phase === 'closed' && previousPhaseRef.current !== 'closed') {
      returnFocusRef.current?.focus({ preventScroll: true });
      returnFocusRef.current = null;
    }
    previousPhaseRef.current = state.phase;
  }, [focusReturnRef, sourceElement, state.phase]);

  useEffect(() => {
    if (state.phase !== 'opening' && state.phase !== 'open' && state.phase !== 'closing') return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (state.active) controller.close(state.active.generation);
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [controller, state.active, state.phase]);

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
        className={styles.owner}
        data-core-task-owner={active.ownerId}
        data-core-task-key={active.key}
        data-core-task-generation={active.generation}
        tabIndex={-1}
        onAnimationEnd={completeAnimation}
        onTransitionEnd={completeAnimation}
      >
        <h2 id={`core-task-heading-${active.generation}`} className={styles.heading}>{heading}</h2>
        <button type="button" className={styles.close} aria-label="关闭任务" onClick={() => controller.close(active.generation)}>
          关闭
        </button>
        <div className={styles.content}>{content}</div>
      </div>
    </section>
  );
}
