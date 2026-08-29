import type { CoreTaskId, CoreTaskRef, TaskLaunchRequest } from './contracts';
import { parseCoreTaskRef } from './contracts';
import { CORE_TASK_REGISTRY, type CoreTaskOwner } from './registry';

export type CoreTaskSurfacePhase = 'closed' | 'opening' | 'open' | 'closing';

export interface ActiveCoreTask {
  readonly ref: CoreTaskRef;
  readonly key: string;
  /** Stable registry owner identifier (ownerId is retained as an explicit alias for callers). */
  readonly owner: string;
  readonly ownerId: string;
  readonly generation: number;
  readonly request: TaskLaunchRequest;
}

export interface CoreTaskSurfaceState {
  readonly phase: CoreTaskSurfacePhase;
  readonly generation: number;
  readonly active: ActiveCoreTask | null;
}

export type CoreTaskLaunchResult =
  | { readonly kind: 'launched'; readonly generation: number; readonly key: string; readonly ownerId: string }
  | { readonly kind: 'focused_existing'; readonly generation: number; readonly key: string; readonly ownerId: string }
  | { readonly kind: 'invalid'; readonly reason: 'unknown_task' | 'invalid_task_identity' }
  | { readonly kind: 'unavailable'; readonly reason: 'task_owner_unavailable' }
  | { readonly kind: 'replacement_denied'; readonly reason: 'replacement_guard_denied'; readonly generation: number }
  | { readonly kind: 'superseded'; readonly generation: number; readonly key: string };

export interface CoreTaskControllerOptions {
  readonly registry?: Partial<Readonly<Record<CoreTaskId, CoreTaskOwner>>> | Readonly<Record<string, CoreTaskOwner | undefined>>;
  /** Return false synchronously to retain the current owner during replacement. */
  readonly canReplace?: (current: ActiveCoreTask, next: CoreTaskRef, request: TaskLaunchRequest) => boolean;
  readonly replacementGuard?: ((current: ActiveCoreTask, next: CoreTaskRef, request: TaskLaunchRequest) => boolean) | {
    readonly canReplace?: (current: ActiveCoreTask, next: CoreTaskRef, request: TaskLaunchRequest) => boolean;
  };
  readonly hasPending?: (current: ActiveCoreTask) => boolean;
  readonly hasUnsavedChanges?: (current: ActiveCoreTask) => boolean;
  readonly onFocus?: (active: ActiveCoreTask) => void;
}

export interface CoreTaskSurfaceController {
  getState(): CoreTaskSurfaceState;
  subscribe(listener: () => void): () => void;
  launch(request: TaskLaunchRequest): CoreTaskLaunchResult;
  focus(generation: number): void;
  subscribeFocus(listener: (active: ActiveCoreTask) => void): () => void;
  markOpen(generation: number): void;
  close(generation: number): void;
  markClosed(generation: number): void;
}

/**
 * Canonical composition helper used by entrypoint adapters. It deliberately
 * only delegates to the injected lifecycle controller: no second controller,
 * transport, Provider, or domain side effect is introduced at the boundary.
 */
export function launchCoreTask(
  controller: Pick<CoreTaskSurfaceController, 'launch'>,
  request: TaskLaunchRequest,
): CoreTaskLaunchResult {
  return controller.launch(request);
}

const CLOSED_STATE: CoreTaskSurfaceState = Object.freeze({ phase: 'closed', generation: 0, active: null });

function frozenRequest(request: TaskLaunchRequest, ref: CoreTaskRef): TaskLaunchRequest {
  const source = request.source;
  const focus = request.focus;
  const hints = request.hints ? Object.freeze({ ...request.hints }) : undefined;
  return Object.freeze({ ref, source, ...(focus ? { focus } : {}), ...(hints ? { hints } : {}) });
}

function ownerFor(
  registry: CoreTaskControllerOptions['registry'],
  taskId: CoreTaskId,
): string | null {
  try {
    const owner = (registry ?? CORE_TASK_REGISTRY)[taskId];
    return owner && typeof owner.ownerId === 'string' && owner.ownerId.trim() ? owner.ownerId : null;
  } catch {
    return null;
  }
}

/**
 * Creates the in-memory lifecycle authority for task surfaces. This module is
 * intentionally limited to identity, focus and display state; it has no
 * Provider, Chat, SSE, HTTP or domain mutation dependencies.
 */
export function createCoreTaskSurfaceController(options: CoreTaskControllerOptions = {}): CoreTaskSurfaceController {
  let state: CoreTaskSurfaceState = CLOSED_STATE;
  const listeners = new Set<() => void>();
  const focusListeners = new Set<(active: ActiveCoreTask) => void>();

  const notify = () => {
    for (const listener of [...listeners]) {
      try {
        listener();
      } catch {
        // Observers are not allowed to break the controller's mutation boundary.
      }
    }
  };

  const setState = (next: CoreTaskSurfaceState) => {
    state = Object.freeze(next);
    notify();
  };

  const launch = (request: TaskLaunchRequest): CoreTaskLaunchResult => {
    let rawRef: unknown;
    try {
      rawRef = request?.ref;
    } catch {
      return { kind: 'invalid', reason: 'invalid_task_identity' };
    }
    const parsed = parseCoreTaskRef(rawRef);
    if (!parsed.ok) return { kind: 'invalid', reason: parsed.reason };

    const ownerId = ownerFor(options.registry, parsed.ref.taskId);
    if (!ownerId) return { kind: 'unavailable', reason: 'task_owner_unavailable' };

    const active = state.active;
    if (active && active.key === parsed.key) {
      try { options.onFocus?.(active); } catch { /* observer isolation */ }
      for (const listener of [...focusListeners]) {
        try { listener(active); } catch { /* observer isolation */ }
      }
      return { kind: 'focused_existing', generation: active.generation, key: active.key, ownerId: active.ownerId };
    }

    if (active) {
      const configuredGuard = options.canReplace
        ?? (typeof options.replacementGuard === 'function' ? options.replacementGuard : options.replacementGuard?.canReplace);
      let allowed = true;
      try {
        if (options.hasPending?.(active)) allowed = false;
      } catch {
        allowed = false;
      }
      if (allowed) {
        try {
          if (options.hasUnsavedChanges?.(active)) allowed = false;
        } catch {
          allowed = false;
        }
      }
      if (allowed && configuredGuard) {
        try {
          if (!configuredGuard(active, parsed.ref, request)) allowed = false;
        } catch {
          allowed = false;
        }
      }
      if (!allowed) {
        return { kind: 'replacement_denied', reason: 'replacement_guard_denied', generation: active.generation };
      }
    }

    const generation = state.generation + 1;
    const canonicalRef = Object.freeze({ ...parsed.ref });
    let normalizedRequest: TaskLaunchRequest;
    try {
      normalizedRequest = frozenRequest(request, canonicalRef);
    } catch {
      return { kind: 'invalid', reason: 'invalid_task_identity' };
    }
    const activeTask: ActiveCoreTask = Object.freeze({
      ref: canonicalRef,
      key: parsed.key,
      owner: ownerId,
      ownerId,
      generation,
      request: normalizedRequest,
    });
    setState({ phase: 'opening', generation, active: activeTask });
    if (state.generation !== generation || state.active?.generation !== generation) {
      return { kind: 'superseded', generation: state.generation, key: parsed.key };
    }
    return { kind: 'launched', generation, key: parsed.key, ownerId };
  };

  const controller: CoreTaskSurfaceController = {
    getState: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    launch,
    focus: (generation) => {
      const active = state.active;
      if (active?.generation === generation) {
        try { options.onFocus?.(active); } catch { /* observer isolation */ }
        for (const listener of [...focusListeners]) {
          try { listener(active); } catch { /* observer isolation */ }
        }
      }
    },
    subscribeFocus: (listener) => {
      focusListeners.add(listener);
      return () => { focusListeners.delete(listener); };
    },
    markOpen: (generation) => {
      if (state.phase !== 'opening' || state.active?.generation !== generation) return;
      setState({ ...state, phase: 'open' });
    },
    close: (generation) => {
      if ((state.phase !== 'opening' && state.phase !== 'open') || state.active?.generation !== generation) return;
      setState({ ...state, phase: 'closing' });
    },
    markClosed: (generation) => {
      if (state.phase !== 'closing' || state.active?.generation !== generation) return;
      setState({ phase: 'closed', generation: state.generation, active: null });
    },
  };
  return controller;
}
