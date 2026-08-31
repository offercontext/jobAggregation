import type { CoreTaskId, CoreTaskRef, TaskLaunchRequest } from './contracts';
import { parseCoreTaskRef } from './contracts';
import { CORE_TASK_REGISTRY, type CoreTaskOwner } from './registry';

export type CoreTaskSurfacePhase = 'closed' | 'opening' | 'open' | 'closing';
export type CoreTaskRecoveryCloseMode = 'ordinary' | 'discard' | 'preserve';

export interface ActiveCoreTask {
  readonly ref: CoreTaskRef;
  readonly key: string;
  /** Stable registry owner identifier (ownerId is retained as an explicit alias for callers). */
  readonly owner: string;
  readonly ownerId: string;
  readonly generation: number;
  /** Exact immediately-closed owner generation eligible for draft recovery. */
  readonly recoveryGeneration: number | null;
  readonly childOwnerIdentity: string | null;
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
  close(generation: number, recoveryMode?: CoreTaskRecoveryCloseMode): void;
  markClosed(generation: number): void;
  /** Explicitly confirms that the owner generation no longer needs retry recovery. */
  settleRecovery(generation: number): void;
  /** Explicitly invalidates retry recovery for an owner generation. */
  revokeRecovery(generation: number): void;
}

export interface CoreTaskCloseGuard {
  readonly pending: boolean;
  readonly unsaved: boolean;
}

/**
 * The single close authority for UI entrypoints. A stale Drawer, keyboard
 * listener, or animation callback cannot close a replacement owner because
 * all canonical and child-owner identity fields must still match.
 */
export function requestCoreTaskClose(
  controller: CoreTaskSurfaceController,
  expected: Pick<ActiveCoreTask, 'generation' | 'key' | 'ownerId' | 'childOwnerIdentity'>,
  guard: CoreTaskCloseGuard,
): boolean {
  const active = controller.getState().active;
  if (
    !active
    || active.generation !== expected.generation
    || active.key !== expected.key
    || active.ownerId !== expected.ownerId
    || active.childOwnerIdentity !== expected.childOwnerIdentity
  ) return false;
  controller.close(active.generation, guard.pending || guard.unsaved ? 'preserve' : 'discard');
  return controller.getState().phase === 'closing'
    && controller.getState().active?.generation === active.generation;
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
  const childOwnerIdentity = request.childOwnerIdentity;
  if (childOwnerIdentity !== undefined && (
    typeof childOwnerIdentity !== 'string'
    || childOwnerIdentity.length === 0
    || childOwnerIdentity.length > 200
  )) throw new Error('invalid_child_owner_identity');
  return Object.freeze({
    ref,
    source,
    ...(focus ? { focus } : {}),
    ...(hints ? { hints } : {}),
    ...(childOwnerIdentity === undefined ? {} : { childOwnerIdentity }),
  });
}

function recoveryCertificateKey(key: string, ownerId: string, childOwnerIdentity: string | null): string {
  return `${ownerId}\u0000${key}\u0000${childOwnerIdentity ?? ''}`;
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
  // This live set is lifecycle-bounded, not cardinality-evicted: every entry
  // represents an explicitly unsettled owner. A numeric FIFO ceiling would
  // make an unknown write unreachable; explicit settlement, revocation, or a
  // guard-cleared discard close are the only safe release operations.
  const closedRecoveryCertificates = new Map<string, ActiveCoreTask>();
  const recoveryCertificateKeyByGeneration = new Map<number, string>();
  const dismissedActiveGenerations = new Set<number>();
  let closingRecoveryIntent: { readonly generation: number; readonly mode: CoreTaskRecoveryCloseMode } | null = null;
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
      let focusedActive = active;
      let normalizedRequest: TaskLaunchRequest;
      try {
        normalizedRequest = frozenRequest(request, active.ref);
      } catch {
        return { kind: 'invalid', reason: 'invalid_task_identity' };
      }
      const childOwnerIdentity = normalizedRequest.childOwnerIdentity ?? null;
      if (childOwnerIdentity !== active.childOwnerIdentity) {
        focusedActive = Object.freeze({
          ...active,
          childOwnerIdentity,
          recoveryGeneration: null,
          request: normalizedRequest,
        });
        setState({ ...state, active: focusedActive });
      }
      try { options.onFocus?.(focusedActive); } catch { /* observer isolation */ }
      for (const listener of [...focusListeners]) {
        try { listener(focusedActive); } catch { /* observer isolation */ }
      }
      return { kind: 'focused_existing', generation: focusedActive.generation, key: focusedActive.key, ownerId: focusedActive.ownerId };
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
    if (active) dismissedActiveGenerations.delete(active.generation);
    const canonicalRef = Object.freeze({ ...parsed.ref });
    let normalizedRequest: TaskLaunchRequest;
    try {
      normalizedRequest = frozenRequest(request, canonicalRef);
    } catch {
      return { kind: 'invalid', reason: 'invalid_task_identity' };
    }
    const childOwnerIdentity = normalizedRequest.childOwnerIdentity ?? null;
    const certificateKey = recoveryCertificateKey(parsed.key, ownerId, childOwnerIdentity);
    const recoveryCertificate = closedRecoveryCertificates.get(certificateKey) ?? null;
    const activeTask: ActiveCoreTask = Object.freeze({
      ref: canonicalRef,
      key: parsed.key,
      owner: ownerId,
      ownerId,
      generation,
      recoveryGeneration: !active ? recoveryCertificate?.generation ?? null : null,
      childOwnerIdentity,
      request: normalizedRequest,
    });
    // A relaunch borrows the exact certificate but never consumes it. Only an
    // explicit owner/draft settlement, revocation, or guard-cleared discard
    // may make an uncertain operation unreachable; repeated close/reopen
    // therefore keeps the original generation available.
    setState({ phase: 'opening', generation, active: activeTask });
    if (state.generation !== generation || state.active?.generation !== generation) {
      return { kind: 'superseded', generation: state.generation, key: parsed.key };
    }
    return { kind: 'launched', generation, key: parsed.key, ownerId };
  };

  const deleteRecoveryCertificate = (certificateKey: string) => {
    const certificate = closedRecoveryCertificates.get(certificateKey);
    if (!certificate) return;
    closedRecoveryCertificates.delete(certificateKey);
    recoveryCertificateKeyByGeneration.delete(certificate.generation);
  };

  const dismissRecovery = (generation: number) => {
    const certificateKey = recoveryCertificateKeyByGeneration.get(generation);
    if (certificateKey) deleteRecoveryCertificate(certificateKey);
    const active = state.active;
    if (active && (active.generation === generation || active.recoveryGeneration === generation)) {
      dismissedActiveGenerations.add(active.generation);
    }
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
    close: (generation, recoveryMode = 'ordinary') => {
      if ((state.phase !== 'opening' && state.phase !== 'open') || state.active?.generation !== generation) return;
      closingRecoveryIntent = Object.freeze({ generation, mode: recoveryMode });
      setState({ ...state, phase: 'closing' });
    },
    markClosed: (generation) => {
      if (state.phase !== 'closing' || state.active?.generation !== generation) return;
      const certificateKey = recoveryCertificateKey(
        state.active.key,
        state.active.ownerId,
        state.active.childOwnerIdentity,
      );
      const recoveryIntent = closingRecoveryIntent;
      closingRecoveryIntent = null;
      const preserveRecovery = recoveryIntent?.generation === generation
        && recoveryIntent.mode === 'preserve';
      const discardRecovery = recoveryIntent?.generation === generation
        && recoveryIntent.mode === 'discard';
      if (dismissedActiveGenerations.delete(state.active.generation) || discardRecovery) {
        deleteRecoveryCertificate(certificateKey);
      } else if (preserveRecovery && !closedRecoveryCertificates.has(certificateKey)) {
        closedRecoveryCertificates.set(certificateKey, state.active);
        recoveryCertificateKeyByGeneration.set(state.active.generation, certificateKey);
      }
      setState({ phase: 'closed', generation: state.generation, active: null });
    },
    settleRecovery: dismissRecovery,
    revokeRecovery: dismissRecovery,
  };
  return controller;
}
