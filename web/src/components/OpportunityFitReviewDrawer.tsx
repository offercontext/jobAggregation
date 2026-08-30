import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Card,
  Divider,
  Drawer,
  Form,
  Input,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import { listResumes } from '@/services/resumes';
import {
  createOpportunityFitV2Triage,
  confirmOpportunityFitV2Triage,
  createOpportunityFitV2DeepReview,
  findOpportunityFitV2SourceConflictStage,
  getOpportunityFitReview,
  getOpportunityFitV2Review,
  listOpportunityFitReviews,
  listOpportunityFitV2Reviews,
} from '@/services/opportunityFitReviews';
import { getApplicationJdVersion } from '@/services/applicationJdVersions';
import type { Application } from '@/types/application';
import type { Resume } from '@/types/resume';
import type {
  OpportunityFitEvidenceRef,
  OpportunityFitReview,
  OpportunityFitV2EvidenceRef,
  OpportunityFitV2Proposal,
  OpportunityFitV2StageResponse,
  OpportunityFitV2Draft,
} from '@/types/opportunityFitReview';
import { createOpportunityFitV2Draft } from '@/types/opportunityFitReview';
import {
  getOpportunityFitErrorMessage,
  OPPORTUNITY_FIT_COPY,
  opportunityFitEvidenceLabel,
  opportunityFitGapKindLabel,
  opportunityFitRecommendationColor,
  opportunityFitRecommendationLabel,
  opportunityFitRecommendedPathLabel,
  opportunityFitStatusLabel,
} from './opportunityFitCopy';
import {
  adaptOpportunityFitHistory,
  type OpportunityFitHistoryItem,
} from '@/features/applicationTasks/opportunityFitHistory';
import { SourceStateTag } from './ui/SourceStateTag';
import workflowStyles from './ui/WorkflowSurface.module.css';

interface Props {
  application: Application | null;
  open: boolean;
  currentJdText?: string;
  jdVersionId?: number | null;
  onClose: () => void;
  onPrepareMaterials?: (reviewOrResumeId: OpportunityFitReview | number, jdText: string, jdVersionId?: number) => void;
  draft?: OpportunityFitV2Draft;
  onDraftChange?: (patch: Partial<OpportunityFitV2Draft> | null) => void;
  /** Composition-only signal for the shared task controller; no draft leaves this owner. */
  onOwnerStateChange?: (state: { pending: boolean; resultUnknown: boolean; unsaved: boolean }) => void;
  /** Bounded read-only projection for Pilot; draft and transport data stay here. */
  onOwnerProjectionChange?: (projection: OpportunityFitOwnerProjection) => void;
  /** Composition-scoped persistence for close/reopen and Application isolation. */
  ownerStore?: OpportunityFitOwnerStore;
  onApplicationMissing?: () => void;
}

export type OpportunityFitOwnerProjectionStatus =
  | 'idle'
  | 'pending'
  | 'result_unknown'
  | 'ready'
  | 'source_conflict'
  | 'unavailable';

export interface OpportunityFitOwnerProjection {
  readonly applicationId: number;
  readonly status: OpportunityFitOwnerProjectionStatus;
  readonly summary: string | null;
  readonly history: readonly Pick<OpportunityFitHistoryItem, 'internalKey' | 'createdAt' | 'summary' | 'sourceState'>[];
  readonly historyState: 'ready' | 'loading' | 'error' | 'absent';
}

export interface OpportunityFitOwnerStore {
  readonly getDraft: (applicationId: number) => OpportunityFitV2Draft;
  readonly setDraft: (applicationId: number, draft: OpportunityFitV2Draft) => void;
  readonly deleteDraft: (applicationId: number) => void;
}

export function createOpportunityFitOwnerStore(): OpportunityFitOwnerStore {
  const drafts = new Map<number, OpportunityFitV2Draft>();
  return Object.freeze({
    getDraft(applicationId: number) {
      const existing = drafts.get(applicationId);
      return existing ? cloneDraft(existing) : cloneDraft(createOpportunityFitV2Draft(applicationId));
    },
    setDraft(applicationId: number, draft: OpportunityFitV2Draft) {
      drafts.set(applicationId, cloneDraft(draft));
    },
    deleteDraft(applicationId: number) {
      drafts.delete(applicationId);
    },
  });
}

function cloneAndFreeze<T>(value: T, seen = new WeakMap<object, unknown>()): T {
  if (value === null || typeof value !== 'object') return value;
  const source = value as object;
  if (seen.has(source)) return seen.get(source) as T;

  const clone = (Array.isArray(value) ? [] : {}) as Record<string, unknown>;
  seen.set(source, clone);
  for (const key of Reflect.ownKeys(source)) {
    if (typeof key !== 'string') continue;
    const descriptor = Object.getOwnPropertyDescriptor(source, key);
    if (descriptor && 'value' in descriptor) {
      clone[key] = cloneAndFreeze(descriptor.value, seen);
    }
  }
  return Object.freeze(clone) as T;
}

function cloneDraft(draft: OpportunityFitV2Draft): OpportunityFitV2Draft {
  return cloneAndFreeze(draft);
}

function isDraftForApplication(
  draft: OpportunityFitV2Draft | undefined,
  applicationId: number,
): draft is OpportunityFitV2Draft {
  try {
    return Boolean(draft && draft.applicationId === applicationId);
  } catch {
    return false;
  }
}

function hasDraftContent(draft: OpportunityFitV2Draft): boolean {
  return Boolean(
    draft.resumeId !== undefined
      || draft.jdText
      || draft.jdVersionId !== undefined
      || draft.assertionsText
      || draft.triageKey
      || draft.deepKey
      || draft.triage
      || draft.deep
      || draft.historical
      || draft.resultUnknown
      || draft.error,
  );
}

function resolveInitialDraft(
  ownerStore: OpportunityFitOwnerStore,
  application: Application | null,
  initialDraftProp: OpportunityFitV2Draft | undefined,
  allowInitialProp: boolean,
): OpportunityFitV2Draft {
  if (!application) return cloneDraft(createOpportunityFitV2Draft(0));
  const stored = ownerStore.getDraft(application.id);
  if (!isDraftForApplication(stored, application.id)) {
    const fresh = cloneDraft(createOpportunityFitV2Draft(application.id));
    ownerStore.setDraft(application.id, fresh);
    return fresh;
  }
  if (allowInitialProp && isDraftForApplication(initialDraftProp, application.id) && !hasDraftContent(stored)) {
    const seeded = cloneDraft(initialDraftProp);
    ownerStore.setDraft(application.id, seeded);
    return seeded;
  }
  return cloneDraft(stored);
}

function isCurrentAttempt(
  attempt: { kind: 'triage' | 'deep_review'; key: string; generation: number } | null,
  kind: 'triage' | 'deep_review',
  key: string,
  generation: number,
): boolean {
  return Boolean(
    attempt
      && attempt.kind === kind
      && attempt.key === key
      && attempt.generation === generation,
  );
}

function EvidenceRefs({ refs }: { refs: OpportunityFitEvidenceRef[] }) {
  if (refs.length === 0) return <Typography.Text type="secondary">{OPPORTUNITY_FIT_COPY.drawer.noDirectEvidence}</Typography.Text>;
  return (
    <Space direction="vertical" size={2} style={{ width: '100%' }}>
      {refs.map((ref) => (
        <Typography.Text key={`${ref.source}:${ref.path}:${ref.excerpt}`} type="secondary">
          {opportunityFitEvidenceLabel(ref.source)} · {ref.path} · “{ref.excerpt}”
        </Typography.Text>
      ))}
    </Space>
  );
}

function ReviewItem({
  title,
  statement,
  refs,
}: {
  title?: string;
  statement: string;
  refs: OpportunityFitEvidenceRef[];
}) {
  return (
    <Card size="small" title={title} style={{ marginBottom: 8 }}>
      <Typography.Paragraph>{statement}</Typography.Paragraph>
      <EvidenceRefs refs={refs} />
    </Card>
  );
}

function V2EvidenceRefs({ refs }: { refs: OpportunityFitV2EvidenceRef[] }) {
  return refs.length > 0 ? (
    <Space direction="vertical" size={2} style={{ width: '100%' }}>
      {refs.map((ref, index) => (
        <Typography.Text key={`${ref.source}:${ref.path}:${index}`} type="secondary">
          {opportunityFitEvidenceLabel(ref.source)} · {ref.path} · “{ref.excerpt}”
        </Typography.Text>
      ))}
    </Space>
  ) : <Typography.Text type="secondary">暂无可验证证据引用</Typography.Text>;
}

function V2ProposalView({ proposal }: { proposal: OpportunityFitV2Proposal }) {
  const sections = [
    ['条件', proposal.conditions],
    ['风险', proposal.risks],
    ['下一步', proposal.next_steps],
  ] as const;
  return (
    <div>
      <Typography.Paragraph>{proposal.summary.text}</Typography.Paragraph>
      <V2EvidenceRefs refs={proposal.summary.evidence_refs} />
      {sections.map(([title, items]) => (
        <section key={title}>
          <Typography.Title level={5}>{title}</Typography.Title>
          {items.length === 0 ? <Typography.Text type="secondary">暂无可验证内容</Typography.Text> : null}
          {items.map((item) => (
            <Card size="small" key={item.id} style={{ marginBottom: 8 }}>
              <Typography.Paragraph>{item.text}</Typography.Paragraph>
              <Typography.Paragraph type="secondary">{item.rationale}</Typography.Paragraph>
              <V2EvidenceRefs refs={item.evidence_refs} />
            </Card>
          ))}
        </section>
      ))}
      <Typography.Title level={5}>待确认问题</Typography.Title>
      {proposal.questions.map((item) => (
        <Card size="small" key={item.question_id} style={{ marginBottom: 8 }}>
          <Typography.Paragraph>{item.text}</Typography.Paragraph>
          <V2EvidenceRefs refs={item.evidence_refs} />
        </Card>
      ))}
    </div>
  );
}

export default function OpportunityFitReviewDrawer({
  application,
  open,
  currentJdText = '',
  jdVersionId,
  onClose,
  onPrepareMaterials,
  draft: initialDraftProp,
  onDraftChange,
  onOwnerStateChange,
  onOwnerProjectionChange,
  ownerStore: ownerStoreProp,
  onApplicationMissing,
}: Props) {
  const localOwnerStoreRef = useRef<OpportunityFitOwnerStore | null>(null);
  if (!localOwnerStoreRef.current) localOwnerStoreRef.current = createOpportunityFitOwnerStore();
  const ownerStore = ownerStoreProp ?? localOwnerStoreRef.current;
  const seededApplicationRef = useRef<number | null>(null);
  const allowInitialProp = Boolean(application && seededApplicationRef.current !== application.id);
  if (application && seededApplicationRef.current !== application.id) seededApplicationRef.current = application.id;
  const initialDraft = resolveInitialDraft(ownerStore, application, initialDraftProp, allowInitialProp);
  const [ownedDraft, setOwnedDraft] = useState<OpportunityFitV2Draft>(initialDraft);
  const ownedDraftRef = useRef(ownedDraft);
  ownedDraftRef.current = ownedDraft;
  const [stage, setStage] = useState<'input' | 'review'>('input');
  const [resumeID, setResumeID] = useState<number | undefined>(initialDraft.resumeId);
  const [jdText, setJdText] = useState(initialDraft.jdText || currentJdText);
  const [assertionsText, setAssertionsText] = useState(initialDraft.assertionsText ?? '');
  const [review, setReview] = useState<OpportunityFitReview | null>(null);
  const [v2Triage, setV2Triage] = useState<OpportunityFitV2StageResponse | null>(initialDraft.triage ?? null);
  const [v2Deep, setV2Deep] = useState<OpportunityFitV2StageResponse | null>(initialDraft.deep ?? null);
  const [v2Historical, setV2Historical] = useState(false);
  const [actionError, setActionError] = useState<string | null>(initialDraft.error ?? null);
  const [historyReadPending, setHistoryReadPending] = useState(false);
  const reviewGenerationRef = useRef(0);
  const historyRequestGenerationRef = useRef(0);
  const triageRequestGenerationRef = useRef(0);
  const confirmRequestGenerationRef = useRef(0);
  const deepRequestGenerationRef = useRef(0);
  const activeAttemptRef = useRef<{ kind: 'triage' | 'deep_review'; key: string; generation: number } | null>(null);
  const onOwnerStateChangeRef = useRef(onOwnerStateChange);
  onOwnerStateChangeRef.current = onOwnerStateChange;
  const onOwnerProjectionChangeRef = useRef(onOwnerProjectionChange);
  onOwnerProjectionChangeRef.current = onOwnerProjectionChange;
  const draft = ownedDraft;

  const emitOwnerState = (nextDraft: OpportunityFitV2Draft = ownedDraftRef.current, pendingOverride?: boolean) => {
    const pending = pendingOverride ?? Boolean(
      !nextDraft.resultUnknown && (
        (nextDraft.triageKey && (!nextDraft.triage || ['generating', 'provider_unknown'].includes(nextDraft.triage.stage_status)))
        || (nextDraft.deepKey && (!nextDraft.deep || ['generating', 'provider_unknown'].includes(nextDraft.deep.stage_status)))
        || activeAttemptRef.current
      ),
    );
    onOwnerStateChangeRef.current?.({
      pending,
      resultUnknown: nextDraft.resultUnknown,
      unsaved: Boolean(nextDraft.resumeId || nextDraft.jdText || nextDraft.assertionsText || nextDraft.triageKey || nextDraft.deepKey || nextDraft.triage || nextDraft.deep),
    });
  };

  const persistDraft = (patch: Partial<OpportunityFitV2Draft> | null) => {
    if (!application) return;
    if (patch === null) {
      ownerStore.deleteDraft(application.id);
      const fresh = cloneDraft(createOpportunityFitV2Draft(application.id));
      setOwnedDraft(fresh);
      onDraftChange?.(null);
      emitOwnerState(fresh, false);
      return;
    }
    const next = cloneDraft({
      ...ownedDraftRef.current,
      ...cloneAndFreeze(patch),
    });
    ownedDraftRef.current = next;
    ownerStore.setDraft(application.id, next);
    setOwnedDraft(next);
    onDraftChange?.(cloneAndFreeze(patch));
    emitOwnerState(next);
  };

  const invalidateHistoryRead = () => {
    historyRequestGenerationRef.current += 1;
    setHistoryReadPending(false);
  };

  const reviewHistoryQuery = useQuery({
    queryKey: ['opportunity-fit-reviews', application?.id],
    queryFn: () => listOpportunityFitReviews(application!.id),
    enabled: open && Boolean(application),
  });

  const resumesQuery = useQuery({
    queryKey: ['resumes'],
    queryFn: () => listResumes(),
    enabled: open,
  });

  const frozenJdQuery = useQuery({
    queryKey: ['application-jd-version', application?.id, v2Deep?.jd_version_id],
    queryFn: () => getApplicationJdVersion(application!.id, v2Deep!.jd_version_id!),
    enabled: open && Boolean(application && v2Deep?.jd_version_id),
  });

  useEffect(() => {
    return () => {
      const activeAttempt = activeAttemptRef.current;
      if (!activeAttempt || !application) return;
      activeAttemptRef.current = null;
      const message = unknownResultCopy;
      const current = ownerStore.getDraft(application.id);
      const next = {
        ...current,
        resultUnknown: true,
        error: message,
        ...(activeAttempt.kind === 'triage' ? { triageKey: activeAttempt.key } : { deepKey: activeAttempt.key }),
      };
      ownerStore.setDraft(application.id, next);
      onOwnerStateChangeRef.current?.({ pending: false, resultUnknown: true, unsaved: true });
      onDraftChange?.({
        resultUnknown: true,
        error: message,
        ...(activeAttempt.kind === 'triage' ? { triageKey: activeAttempt.key } : { deepKey: activeAttempt.key }),
      });
    };
  }, [application?.id]);

  useEffect(() => {
    reviewGenerationRef.current += 1;
    if (!open) return;
    const nextDraft = resolveInitialDraft(ownerStore, application, undefined, false);
    setOwnedDraft(nextDraft);
    setStage(nextDraft.triage || nextDraft.deep ? 'review' : 'input');
    setResumeID(nextDraft.resumeId);
    setJdText(nextDraft.jdText || currentJdText);
    setAssertionsText(nextDraft.assertionsText ?? '');
    setReview(null);
    setV2Triage(nextDraft.triage ?? null);
    setV2Deep(nextDraft.deep ?? null);
    setV2Historical(Boolean(nextDraft.historical));
    setActionError(nextDraft.error ?? null);
    emitOwnerState(nextDraft);
  // The composition-scoped owner store is the source of truth across
  // unmount/remount. Do not reset it on ordinary parent renders or current-JD
  // query refreshes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [application?.id, open]);

  useEffect(() => {
    if (!open || stage !== 'input' || v2Triage || v2Deep || v2Historical) return;
    const hasActiveAttempt = Boolean(draft?.triageKey || draft?.deepKey);
    if (!hasActiveAttempt && currentJdText && draft?.jdVersionId !== jdVersionId) {
      setJdText(currentJdText);
      persistDraft({ jdText: currentJdText, jdVersionId: jdVersionId ?? undefined });
    }
  }, [currentJdText, draft?.deepKey, draft?.jdVersionId, draft?.triageKey, jdVersionId, onDraftChange, open, stage, v2Deep, v2Historical, v2Triage]);

  const assertions = useMemo(
    () => assertionsText.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
    [assertionsText],
  );
  const assertionError = assertions.length > 10
    ? OPPORTUNITY_FIT_COPY.drawer.assertionsTooMany
    : assertions.some((value) => value.length > 500)
      ? OPPORTUNITY_FIT_COPY.drawer.assertionsTooLong
      : null;

  const errorCode = (error: unknown): string | undefined => {
    if (!error || typeof error !== 'object') return undefined;
    const candidate = error as { response?: { data?: { error_code?: unknown } }; code?: unknown };
    const responseCode = candidate.response?.data?.error_code;
    return typeof responseCode === 'string'
      ? responseCode
      : typeof candidate.code === 'string' ? candidate.code : undefined;
  };

  const isProviderUnknown = (error: unknown): boolean => {
    const code = errorCode(error);
    if (code === 'opportunity_fit_unverifiable') return false;
    if (code === 'opportunity_fit_provider_error') return true;
    if (!error || typeof error !== 'object') return true;
    const response = (error as { response?: unknown }).response;
    if (!response || typeof response !== 'object') return true;
    const status = (response as { status?: unknown }).status;
    return typeof status === 'number' && status >= 500;
  };

  const unknownResultCopy = '操作结果待确认，请使用原尝试重试。';
  const historyUnavailableCopy = '部分历史暂时不可用';

  const recoverConfirmedTriage = async (generation: number, attemptKey: string): Promise<boolean> => {
    if (!application || !v2Triage) return false;
    try {
      const session = await getOpportunityFitV2Review(application.id, v2Triage.review_id);
      const current = session.stages.find((item) => (
        item.stage === 'triage' && item.stage_id === v2Triage.stage_id
      )) ?? session.stages.find((item) => item.stage === 'triage');
      if (current?.stage_status !== 'confirmed') return false;
      if (
        generation !== reviewGenerationRef.current
        || !isCurrentAttempt(activeAttemptRef.current, 'triage', attemptKey, generation)
      ) return false;
      activeAttemptRef.current = null;
      setV2Triage(current);
      setStage('review');
      setActionError(null);
      persistDraft({ triage: current, resultUnknown: false, error: null });
      return true;
    } catch {
      return false;
    }
  };

  const isConfirmationConsumed = (error: unknown): boolean => (
    errorCode(error) === 'opportunity_fit_triage_confirmation_consumed'
  );

  const isConfirmationExpired = (error: unknown): boolean => (
    errorCode(error) === 'opportunity_fit_triage_confirmation_expired'
  );

  const isNotFound = (error: unknown): boolean => {
    if (!error || typeof error !== 'object') return false;
    try {
      return (error as { response?: { status?: unknown } }).response?.status === 404;
    } catch {
      return false;
    }
  };

  const isSourceConflict = (error: unknown): boolean => {
    const code = errorCode(error);
    return code === 'application_jd_source_conflict' || code === 'opportunity_fit_source_conflict';
  };

  const sourceConflictCopy = OPPORTUNITY_FIT_COPY.drawer.sourceChanged;

  const recoverSourceConflict = async (
    stage: 'triage' | 'deep_review',
    idempotencyKey: string,
    reviewID?: number,
  ): Promise<Awaited<ReturnType<typeof findOpportunityFitV2SourceConflictStage>>> => {
    if (!application) return { status: 'not_found' };
    try {
      return await findOpportunityFitV2SourceConflictStage(application.id, stage, idempotencyKey, reviewID);
    } catch {
      return { status: 'unknown' };
    }
  };

  const createMutation = useMutation({
    mutationFn: (input: Parameters<typeof createOpportunityFitV2Triage>[1]) => (
      createOpportunityFitV2Triage(application!.id, input)
    ),
    onSuccess: (nextReview) => {
      const generation = triageRequestGenerationRef.current;
      if (
        generation !== reviewGenerationRef.current
        || !isCurrentAttempt(activeAttemptRef.current, 'triage', nextReview.idempotency_key, generation)
      ) return;
      activeAttemptRef.current = null;
      setV2Triage(nextReview);
      setV2Deep(null);
      setStage('review');
      setActionError(null);
      persistDraft({
        triage: nextReview,
        deep: null,
        triageKey: nextReview.idempotency_key,
        resultUnknown: ['generating', 'provider_unknown'].includes(nextReview.stage_status),
        error: ['generating', 'provider_unknown'].includes(nextReview.stage_status) ? unknownResultCopy : null,
      });
    },
    onError: async (error, input) => {
      const generation = triageRequestGenerationRef.current;
      if (
        !input
        || generation !== reviewGenerationRef.current
        || !isCurrentAttempt(activeAttemptRef.current, 'triage', input.idempotency_key, generation)
      ) return;
      if (isSourceConflict(error)) {
        const conflict = await recoverSourceConflict('triage', input.idempotency_key);
        if (
          generation !== reviewGenerationRef.current
          || !isCurrentAttempt(activeAttemptRef.current, 'triage', input.idempotency_key, generation)
        ) return;
        if (conflict.status === 'found') {
          activeAttemptRef.current = null;
          setV2Triage(conflict.stage);
          setStage('review');
          setActionError(sourceConflictCopy);
          persistDraft({
            triage: conflict.stage,
            triageKey: null,
            resultUnknown: false,
            error: sourceConflictCopy,
          });
          return;
        }
        if (conflict.status === 'unknown') {
          activeAttemptRef.current = null;
          setActionError(unknownResultCopy);
          persistDraft({
            resultUnknown: true,
            error: unknownResultCopy,
            triageKey: input.idempotency_key,
          });
          return;
        }
        if (conflict.status === 'application_missing') {
          activeAttemptRef.current = null;
          persistDraft(null);
          onApplicationMissing?.();
          onClose();
          return;
        }
        if (conflict.status === 'review_missing') {
          activeAttemptRef.current = null;
          resetV2Review('当前投递或岗位评估已不存在，请重新打开。');
          return;
        }
      }
      if (isNotFound(error)) {
        activeAttemptRef.current = null;
        persistDraft(null);
        onApplicationMissing?.();
        onClose();
        return;
      }
      const unknown = isProviderUnknown(error);
      activeAttemptRef.current = null;
      setActionError(unknown ? unknownResultCopy : getOpportunityFitErrorMessage(error));
      persistDraft({
        resultUnknown: unknown,
        error: unknown ? unknownResultCopy : getOpportunityFitErrorMessage(error),
        triageKey: unknown ? input.idempotency_key : null,
      });
    },
  });
  const v2HistoryQuery = useQuery({
    queryKey: ['opportunity-fit-v2-reviews', application?.id],
    queryFn: () => listOpportunityFitV2Reviews(application!.id),
    enabled: open && Boolean(application),
  });

  const historyProjection = useMemo(() => adaptOpportunityFitHistory({
    applicationId: application?.id,
    currentSourceFingerprint: v2Deep?.source_fingerprint_sha256 ?? v2Triage?.source_fingerprint_sha256,
    v1: reviewHistoryQuery.error
      ? { status: 'error', reason: 'v1_history_error' }
      : reviewHistoryQuery.data === undefined
        ? { status: 'loading' }
        : { status: 'ready', value: reviewHistoryQuery.data },
    v2: v2HistoryQuery.error
      ? { status: 'error', reason: 'v2_history_error' }
      : v2HistoryQuery.data === undefined
        ? { status: 'loading' }
        : { status: 'ready', value: v2HistoryQuery.data },
  }), [application?.id, reviewHistoryQuery.data, reviewHistoryQuery.error, v2Deep?.source_fingerprint_sha256, v2HistoryQuery.data, v2HistoryQuery.error, v2Triage?.source_fingerprint_sha256]);

  const historySourceByKey = useMemo(() => {
    const map = new Map<string, { source: 'v1' | 'v2'; id: number }>();
    historyProjection.items.forEach((item) => {
      const [source, applicationId, id] = item.internalKey.split(':');
      const numericId = Number(id);
      if ((source === 'v1' || source === 'v2')
        && Number(applicationId) === application?.id
        && Number.isSafeInteger(numericId)
        && numericId > 0) {
        map.set(item.internalKey, { source, id: numericId });
      }
    });
    return map;
  }, [application?.id, historyProjection.items]);

  const v2HasSourceConflict = v2Triage?.stage_status === 'source_conflict'
    || v2Deep?.stage_status === 'source_conflict';

  const ownerProjection = useMemo<OpportunityFitOwnerProjection>(() => {
    const summary = v2Deep?.proposal?.summary.text
      ?? v2Triage?.proposal?.summary.text
      ?? review?.triage.summary.text
      ?? null;
    const historyState: OpportunityFitOwnerProjection['historyState'] = reviewHistoryQuery.error || v2HistoryQuery.error
      ? 'error'
      : reviewHistoryQuery.data === undefined || v2HistoryQuery.data === undefined
        ? 'loading'
        : historyProjection.partial
          ? 'error'
        : 'ready';
    const pending = Boolean(
      activeAttemptRef.current
      || (draft.triageKey && (!draft.triage || ['generating', 'provider_unknown'].includes(draft.triage.stage_status)))
      || (draft.deepKey && (!draft.deep || ['generating', 'provider_unknown'].includes(draft.deep.stage_status))),
    );
    const status: OpportunityFitOwnerProjectionStatus = draft.resultUnknown
      ? 'result_unknown'
      : v2HasSourceConflict
        ? 'source_conflict'
        : pending
          ? 'pending'
          : summary
            ? 'ready'
            : actionError
              ? 'unavailable'
              : 'idle';
    return Object.freeze({
      applicationId: application?.id ?? 0,
      status,
      summary,
      history: historyProjection.items,
      historyState,
    });
  }, [actionError, application?.id, draft, historyProjection.items, review, reviewHistoryQuery.data, reviewHistoryQuery.error, v2Deep, v2HasSourceConflict, v2HistoryQuery.data, v2HistoryQuery.error, v2Triage]);

  useEffect(() => {
    if (application && ownerProjection.applicationId === application.id) {
      onOwnerProjectionChangeRef.current?.(ownerProjection);
    }
  }, [application, ownerProjection]);

  const confirmV2Mutation = useMutation({
    mutationFn: () => confirmOpportunityFitV2Triage(
      application!.id,
      v2Triage!.review_id,
      v2Triage!.stage_id,
      v2Triage!.confirmation_token!,
    ),
    onSuccess: (nextReview) => {
      const generation = confirmRequestGenerationRef.current;
      const attemptKey = draft.triageKey ?? v2Triage?.idempotency_key;
      if (
        !attemptKey
        || generation !== reviewGenerationRef.current
        || !isCurrentAttempt(activeAttemptRef.current, 'triage', attemptKey, generation)
      ) return;
      activeAttemptRef.current = null;
      setV2Triage(nextReview);
      persistDraft({ triage: nextReview, resultUnknown: false, error: null });
    },
    onError: async (error) => {
      const generation = confirmRequestGenerationRef.current;
      const attemptKey = draft.triageKey ?? v2Triage?.idempotency_key;
      if (
        !attemptKey
        || generation !== reviewGenerationRef.current
        || !isCurrentAttempt(activeAttemptRef.current, 'triage', attemptKey, generation)
      ) return;
      if (isConfirmationExpired(error)) {
        activeAttemptRef.current = null;
        resetV2Review(getOpportunityFitErrorMessage(error));
        return;
      }
      if (isConfirmationConsumed(error) || isProviderUnknown(error)) {
        if (await recoverConfirmedTriage(generation, attemptKey)) return;
        if (
          generation !== reviewGenerationRef.current
          || !isCurrentAttempt(activeAttemptRef.current, 'triage', attemptKey, generation)
        ) return;
        activeAttemptRef.current = null;
        setActionError(unknownResultCopy);
        persistDraft({ resultUnknown: true, error: unknownResultCopy });
        return;
      }
      if (isNotFound(error)) {
        activeAttemptRef.current = null;
        persistDraft(null);
        onApplicationMissing?.();
        onClose();
        return;
      }
      const message = getOpportunityFitErrorMessage(error);
      if (draft?.resultUnknown) {
        activeAttemptRef.current = null;
        resetV2Review(message);
        return;
      }
      activeAttemptRef.current = null;
      setActionError(message);
    },
  });

  const deepReviewMutation = useMutation<
    OpportunityFitV2StageResponse,
    unknown,
    Parameters<typeof createOpportunityFitV2DeepReview>[2]
  >({
    mutationFn: (input: Parameters<typeof createOpportunityFitV2DeepReview>[2]) => {
      if (!v2Triage) throw new Error('Triage is required');
      return createOpportunityFitV2DeepReview(application!.id, v2Triage.review_id, input);
    },
    onSuccess: (nextReview) => {
      const generation = deepRequestGenerationRef.current;
      if (
        generation !== reviewGenerationRef.current
        || !isCurrentAttempt(activeAttemptRef.current, 'deep_review', nextReview.idempotency_key, generation)
      ) return;
      activeAttemptRef.current = null;
      setV2Deep(nextReview);
      setActionError(null);
      persistDraft({
        deep: nextReview,
        deepKey: nextReview.idempotency_key,
        resultUnknown: ['generating', 'provider_unknown'].includes(nextReview.stage_status),
        error: ['generating', 'provider_unknown'].includes(nextReview.stage_status) ? unknownResultCopy : null,
      });
    },
    onError: async (error, input) => {
      const generation = deepRequestGenerationRef.current;
      if (
        !input
        || generation !== reviewGenerationRef.current
        || !isCurrentAttempt(activeAttemptRef.current, 'deep_review', input.idempotency_key, generation)
      ) return;
      if (isSourceConflict(error)) {
        const conflict = await recoverSourceConflict(
          'deep_review',
          input.idempotency_key,
          v2Triage?.review_id,
        );
        if (
          generation !== reviewGenerationRef.current
          || !isCurrentAttempt(activeAttemptRef.current, 'deep_review', input.idempotency_key, generation)
        ) return;
        if (conflict.status === 'found') {
          activeAttemptRef.current = null;
          setV2Deep(conflict.stage);
          setActionError(sourceConflictCopy);
          persistDraft({
            deep: conflict.stage,
            deepKey: null,
            resultUnknown: false,
            error: sourceConflictCopy,
          });
          return;
        }
        if (conflict.status === 'unknown') {
          activeAttemptRef.current = null;
          setActionError(unknownResultCopy);
          persistDraft({
            resultUnknown: true,
            error: unknownResultCopy,
            deepKey: input.idempotency_key,
          });
          return;
        }
        if (conflict.status === 'application_missing') {
          activeAttemptRef.current = null;
          persistDraft(null);
          onApplicationMissing?.();
          onClose();
          return;
        }
        if (conflict.status === 'review_missing') {
          activeAttemptRef.current = null;
          resetV2Review('当前投递或岗位评估已不存在，请重新打开。');
          return;
        }
      }
      if (isNotFound(error)) {
        activeAttemptRef.current = null;
        persistDraft(null);
        onApplicationMissing?.();
        onClose();
        return;
      }
      const unknown = isProviderUnknown(error);
      activeAttemptRef.current = null;
      setActionError(unknown ? unknownResultCopy : getOpportunityFitErrorMessage(error));
      persistDraft({
        resultUnknown: unknown,
        error: unknown ? unknownResultCopy : getOpportunityFitErrorMessage(error),
        deepKey: unknown ? input.idempotency_key : null,
      });
    },
  });

  const canSubmit = Boolean(
    application
      && (draft?.triageKey ? draft.resumeId && draft.jdVersionId : resumeID && jdVersionId)
      && !assertionError
      && !createMutation.isPending,
  );

  const persistedStagePending = [draft?.triage?.stage_status, draft?.deep?.stage_status]
    .some((status) => status === 'generating' || status === 'provider_unknown');
  const persistedAttemptPending = Boolean(
    (draft?.triageKey && !v2Triage) || (draft?.deepKey && !v2Deep),
  );
  const historyButtonsDisabled = createMutation.isPending
    || confirmV2Mutation.isPending
    || deepReviewMutation.isPending
    || Boolean(draft?.resultUnknown)
    || persistedStagePending
    || persistedAttemptPending;

  const buildTriageInput = () => {
    const frozen = Boolean(draft?.triageKey);
    const selectedResumeID = frozen ? draft?.resumeId : resumeID;
    const selectedJdVersionId = frozen ? draft?.jdVersionId : jdVersionId;
    if (!selectedResumeID || !selectedJdVersionId) return null;
    return {
      schema_version: 2 as const,
      resume_id: selectedResumeID,
      jd_version_id: selectedJdVersionId,
      jd_source_label: OPPORTUNITY_FIT_COPY.drawer.jdSourceLabel,
      candidate_assertions: frozen
        ? (draft?.assertionsText ?? '').split(/\r?\n/).map((value) => value.trim()).filter(Boolean)
        : assertions,
      idempotency_key: draft?.triageKey ?? crypto.randomUUID(),
    };
  };

  const submit = () => {
    if (v2Historical || !canSubmit || assertionError) return;
    const input = buildTriageInput();
    if (!input) return;
    invalidateHistoryRead();
    activeAttemptRef.current = { kind: 'triage', key: input.idempotency_key, generation: reviewGenerationRef.current };
    persistDraft({
      resumeId: input.resume_id,
      jdText,
      jdVersionId: input.jd_version_id,
      assertionsText,
      triageKey: input.idempotency_key,
      resultUnknown: false,
      error: null,
    });
    triageRequestGenerationRef.current = reviewGenerationRef.current;
    createMutation.mutate(input);
  };

  const submitDeepReview = () => {
    if (v2Historical || !v2Triage || v2Triage.stage_status !== 'confirmed' || !v2Triage.jd_version_id || !resumeID) return;
    invalidateHistoryRead();
    const input = {
      schema_version: 2 as const,
      resume_id: draft?.deepKey ? (draft.resumeId ?? v2Triage.resume_id ?? resumeID) : (v2Triage.resume_id ?? resumeID),
      jd_source_label: OPPORTUNITY_FIT_COPY.drawer.jdSourceLabel,
      candidate_assertions: draft?.deepKey
        ? (draft.assertionsText ?? '').split(/\r?\n/).map((value) => value.trim()).filter(Boolean)
        : assertions,
      idempotency_key: draft?.deepKey ?? crypto.randomUUID(),
      parent_triage_stage_id: v2Triage.stage_id,
    };
    activeAttemptRef.current = { kind: 'deep_review', key: input.idempotency_key, generation: reviewGenerationRef.current };
    // Persist the key before the request leaves the page. If the response is
    // lost during unmount, this owner can still replay this exact attempt.
    persistDraft({
      resumeId: input.resume_id,
      jdVersionId: v2Triage.jd_version_id,
      assertionsText,
      deepKey: input.idempotency_key,
      resultUnknown: false,
      error: null,
    });
    deepRequestGenerationRef.current = reviewGenerationRef.current;
    deepReviewMutation.mutate(input);
  };

  const openHistoricalReview = async (reviewID: number) => {
    if (!application) return;
    const generation = reviewGenerationRef.current;
    const requestGeneration = ++historyRequestGenerationRef.current;
    setHistoryReadPending(true);
    try {
      setActionError(null);
      const historicalReview = await getOpportunityFitReview(application.id, reviewID);
      if (generation !== reviewGenerationRef.current || requestGeneration !== historyRequestGenerationRef.current) return;
      setResumeID(historicalReview.source.resume.id);
      setJdText(historicalReview.source.jd.text);
      setAssertionsText(historicalReview.source.candidate_assertions.map((item) => item.text).join('\n'));
      setReview(historicalReview);
      setV2Triage(null);
      setV2Deep(null);
      setV2Historical(false);
      setStage('review');
    } catch {
      if (generation !== reviewGenerationRef.current || requestGeneration !== historyRequestGenerationRef.current) return;
      setActionError(historyUnavailableCopy);
    } finally {
      if (generation === reviewGenerationRef.current && requestGeneration === historyRequestGenerationRef.current) {
        setHistoryReadPending(false);
      }
    }
  };

  const openHistoricalV2Review = async (reviewID: number) => {
    if (!application) return;
    const generation = reviewGenerationRef.current;
    const requestGeneration = ++historyRequestGenerationRef.current;
    setHistoryReadPending(true);
    try {
      const historical = await getOpportunityFitV2Review(application.id, reviewID);
      if (generation !== reviewGenerationRef.current || requestGeneration !== historyRequestGenerationRef.current) return;
      const triage = historical.stages.find((item) => item.stage === 'triage') ?? null;
      const deep = historical.stages.find((item) => item.stage === 'deep_review') ?? null;
      setReview(null);
      setV2Triage(triage);
      setV2Deep(deep);
      setV2Historical(true);
      setStage('review');
      setActionError(null);
    } catch {
      if (generation !== reviewGenerationRef.current || requestGeneration !== historyRequestGenerationRef.current) return;
      setActionError(historyUnavailableCopy);
    } finally {
      if (generation === reviewGenerationRef.current && requestGeneration === historyRequestGenerationRef.current) {
        setHistoryReadPending(false);
      }
    }
  };

  const resetV2Review = (message?: string) => {
    if (v2Historical) return;
    reviewGenerationRef.current += 1;
    historyRequestGenerationRef.current += 1;
    setHistoryReadPending(false);
    activeAttemptRef.current = null;
    persistDraft(null);
    setStage('input');
    setResumeID(undefined);
    setJdText(currentJdText);
    setAssertionsText('');
    setReview(null);
    setV2Triage(null);
    setV2Deep(null);
    setV2Historical(false);
    setActionError(message ?? null);
  };

  const canStartNewV2Review = Boolean(
    ['ready', 'confirmed', 'source_conflict'].includes(v2Triage?.stage_status ?? '')
      || ['ready', 'confirmed', 'source_conflict'].includes(v2Deep?.stage_status ?? ''),
  );
  const historyErrorMessage = reviewHistoryQuery.error
    ? getOpportunityFitErrorMessage(reviewHistoryQuery.error)
    : v2HistoryQuery.error
      ? getOpportunityFitErrorMessage(v2HistoryQuery.error)
      : null;
  const handleClose = () => {
    const activeAttempt = activeAttemptRef.current;
    if (activeAttempt && application) {
      activeAttemptRef.current = null;
      persistDraft({
        resultUnknown: true,
        error: unknownResultCopy,
        ...(activeAttempt.kind === 'triage' ? { triageKey: activeAttempt.key } : { deepKey: activeAttempt.key }),
      });
    }
    onClose();
  };

  if (!open) return null;

  return (
    <Drawer
      open={open}
      width={680}
      title={OPPORTUNITY_FIT_COPY.drawer.title}
      onClose={handleClose}
      destroyOnClose
    >
      <div className={`${workflowStyles.surface} ${workflowStyles.stack}`}>
      <Typography.Paragraph type="secondary">
        {OPPORTUNITY_FIT_COPY.drawer.description}
      </Typography.Paragraph>
      {actionError ? <Alert type="error" showIcon message={actionError} /> : null}
      {stage === 'input' ? (
        <Card size="small" title={OPPORTUNITY_FIT_COPY.drawer.history} className={workflowStyles.section} style={{ marginBottom: 16 }}>
          {historyProjection.partial ? (
            <Alert
              type="warning"
              showIcon
              message={historyErrorMessage ? `${historyErrorMessage}；部分历史暂时不可用` : '部分历史暂时不可用'}
            />
          ) : null}
          {reviewHistoryQuery.data === undefined && v2HistoryQuery.data === undefined ? (
            <Typography.Text type="secondary">历史记录加载中</Typography.Text>
          ) : historyProjection.items.length === 0 && !historyProjection.partial ? (
            <Typography.Text type="secondary">暂无历史记录</Typography.Text>
          ) : null}
          <fieldset disabled={historyButtonsDisabled} style={{ border: 0, padding: 0, margin: 0 }}>
            <Space direction="vertical" style={{ width: '100%' }}>
              {historyProjection.items.map((item: OpportunityFitHistoryItem) => {
                const reference = historySourceByKey.get(item.internalKey);
                return (
                  <Space key={item.internalKey} className={workflowStyles.listRow} style={{ justifyContent: 'space-between', width: '100%' }}>
                    <div>
                      <Typography.Text>{item.summary}</Typography.Text>
                      <br />
                      <Typography.Text type="secondary">
                        {item.sourceState === 'source_changed' ? '来源已更新，已有结果仍保留' : '当前来源'} · {new Date(item.createdAt).toLocaleString()}
                      </Typography.Text>
                    </div>
                    {reference ? (
                      <Button
                        size="small"
                        disabled={historyButtonsDisabled}
                        onClick={() => void (reference.source === 'v1'
                          ? openHistoricalReview(reference.id)
                          : openHistoricalV2Review(reference.id))}
                      >
                        {OPPORTUNITY_FIT_COPY.drawer.view}
                      </Button>
                    ) : null}
                  </Space>
                );
              })}
            </Space>
          </fieldset>
        </Card>
      ) : null}

      {stage === 'input' ? (
        <div data-testid="opportunity-fit-source-panel" className={workflowStyles.section}>
        <Form layout="vertical">
          <Form.Item label={OPPORTUNITY_FIT_COPY.drawer.resumeLabel} required>
            <Select
              value={resumeID}
              disabled={Boolean(draft?.resultUnknown)}
              onChange={(value) => {
                setResumeID(value as number);
                persistDraft({ resumeId: value as number });
              }}
              loading={resumesQuery.isFetching}
              placeholder={OPPORTUNITY_FIT_COPY.drawer.resumePlaceholder}
              options={(resumesQuery.data || []).map((resume: Resume) => ({
                value: resume.id,
                label: resume.name || resume.title,
              }))}
            />
          </Form.Item>
          <Form.Item label={OPPORTUNITY_FIT_COPY.drawer.jdLabel} required>
            <Input.TextArea
              value={jdText}
              rows={9}
              readOnly
              aria-readonly="true"
              placeholder={OPPORTUNITY_FIT_COPY.drawer.jdPlaceholder}
            />
            <Typography.Text type="secondary">
              {currentJdText ? '使用投递当前已确认的岗位资料；如需修改，请先返回 JD 版本入口。' : '当前投递尚未确认岗位资料，请先保存 JD 版本。'}
            </Typography.Text>
          </Form.Item>
          <Form.Item label={OPPORTUNITY_FIT_COPY.drawer.assertionsLabel}>
            <Input.TextArea
              value={assertionsText}
              disabled={Boolean(draft?.resultUnknown)}
              onChange={(event) => {
                setAssertionsText(event.target.value);
                persistDraft({ assertionsText: event.target.value });
              }}
              rows={5}
              placeholder={OPPORTUNITY_FIT_COPY.drawer.assertionsPlaceholder}
            />
            <Typography.Text type="secondary">{OPPORTUNITY_FIT_COPY.drawer.assertionsHint}</Typography.Text>
            {assertionError ? <Typography.Text type="danger">{assertionError}</Typography.Text> : null}
          </Form.Item>
          <Alert
            type="info"
            showIcon
            message={OPPORTUNITY_FIT_COPY.drawer.humanConfirmation}
            description={OPPORTUNITY_FIT_COPY.drawer.humanConfirmationDescription}
          />
          <div data-testid="opportunity-fit-action-group" className={workflowStyles.actionGroup}>
            <Button type="primary" onClick={submit} loading={createMutation.isPending} disabled={!canSubmit}>
              {draft?.triageKey ? '使用原尝试重试' : OPPORTUNITY_FIT_COPY.drawer.startTriage}
            </Button>
          </div>
        </Form>
        </div>
      ) : v2Triage ? (
        <div className={`${workflowStyles.section} op-long-text`}>
          <Space wrap>
            <Tag color="blue">岗位判断</Tag>
            <SourceStateTag
              state={v2HasSourceConflict ? 'changed' : 'frozen'}
              detail={v2HasSourceConflict ? OPPORTUNITY_FIT_COPY.drawer.sourceChanged : OPPORTUNITY_FIT_COPY.drawer.sourceFrozen}
            />
            <Tag>{OPPORTUNITY_FIT_COPY.drawer.humanConfirmation}</Tag>
          </Space>
          <Typography.Title level={4}>{OPPORTUNITY_FIT_COPY.drawer.triage}</Typography.Title>
          {v2Triage.stage_status === 'source_conflict' ? (
            <Alert type="warning" showIcon message={OPPORTUNITY_FIT_COPY.drawer.sourceChanged} />
          ) : v2Triage.proposal ? <V2ProposalView proposal={v2Triage.proposal} /> : <Spin />}
          {!v2Historical && ['generating', 'provider_unknown'].includes(v2Triage.stage_status) ? (
            <Button type="primary" onClick={submit} loading={createMutation.isPending} disabled={!canSubmit}>
              使用原尝试重试
            </Button>
          ) : null}
          {!v2Historical && v2Triage.stage_status === 'ready' && v2Triage.confirmation_token ? (
            <Button
              type="primary"
              onClick={() => {
                if (v2Historical) return;
                invalidateHistoryRead();
                activeAttemptRef.current = {
                  kind: 'triage',
                  key: draft.triageKey ?? v2Triage.idempotency_key,
                  generation: reviewGenerationRef.current,
                };
                persistDraft({ resultUnknown: false, error: null });
                confirmRequestGenerationRef.current = reviewGenerationRef.current;
                confirmV2Mutation.mutate();
              }}
              loading={confirmV2Mutation.isPending}
            >
              {draft?.resultUnknown ? '使用原尝试重试' : '确认快速判断'}
            </Button>
          ) : null}
          {!v2Historical && v2Triage.stage_status === 'confirmed' && !v2Deep ? (
            <Button
              type="primary"
              onClick={submitDeepReview}
              loading={deepReviewMutation.isPending}
            >
              {draft?.deepKey ? '使用原尝试重试' : OPPORTUNITY_FIT_COPY.drawer.startDeepReview}
            </Button>
          ) : null}
          {v2Deep ? (
            <>
              <Divider />
              <Typography.Title level={4}>{OPPORTUNITY_FIT_COPY.drawer.deepReview}</Typography.Title>
              {v2Deep.stage_status === 'source_conflict' ? (
                <Alert type="warning" showIcon message={OPPORTUNITY_FIT_COPY.drawer.sourceChanged} />
              ) : v2Deep.proposal ? <V2ProposalView proposal={v2Deep.proposal} /> : <Spin />}
              {!v2Historical && ['generating', 'provider_unknown'].includes(v2Deep.stage_status) ? (
                <Button type="primary" onClick={submitDeepReview} loading={deepReviewMutation.isPending}>
                  使用原尝试重试
                </Button>
              ) : null}
              {v2Deep.jd_version_id !== jdVersionId ? (
                <Alert type="warning" showIcon message={`${OPPORTUNITY_FIT_COPY.drawer.sourceChanged} 请重新开始评估。`} />
              ) : null}
              <Button
                type="primary"
                onClick={() => onPrepareMaterials?.(
                  v2Deep.resume_id ?? resumeID!,
                  (frozenJdQuery.data as { jd_text?: string } | undefined)?.jd_text ?? '',
                  v2Deep.jd_version_id ?? undefined,
                )}
                disabled={
                  !onPrepareMaterials
                  || !v2Deep.resume_id
                  || !v2Deep.jd_version_id
                  || v2Historical
                  || !frozenJdQuery.data
                  || v2Deep.jd_version_id !== jdVersionId
                }
              >
                {OPPORTUNITY_FIT_COPY.drawer.prepareMaterials}
              </Button>
            </>
          ) : null}
          {!v2Historical && canStartNewV2Review && !draft?.resultUnknown ? (
            <Button
              disabled={historyReadPending || createMutation.isPending || confirmV2Mutation.isPending || deepReviewMutation.isPending}
              onClick={() => resetV2Review()}
            >重新开始岗位评估</Button>
          ) : null}
        </div>
      ) : review ? (
        <div className={`${workflowStyles.section} op-long-text`}>
          <Space wrap>
            <Tag color={opportunityFitRecommendationColor(review.recommendation)}>
              {opportunityFitRecommendationLabel(review.recommendation)}
            </Tag>
            <SourceStateTag state="frozen" detail={OPPORTUNITY_FIT_COPY.drawer.sourceFrozen} />
            <Tag>{OPPORTUNITY_FIT_COPY.drawer.humanConfirmation}</Tag>
          </Space>
          <Typography.Title level={4}>{OPPORTUNITY_FIT_COPY.drawer.triage}</Typography.Title>
          <Typography.Paragraph>{review.triage.summary.text}</Typography.Paragraph>
          <EvidenceRefs refs={review.triage.summary.evidence_refs} />

          <Typography.Title level={5}>{OPPORTUNITY_FIT_COPY.drawer.hardConstraints}</Typography.Title>
          {review.triage.hard_constraints.map((item) => (
            <ReviewItem
              key={item.id}
              title={`${item.requirement} · ${opportunityFitStatusLabel(item.status)}`}
              statement={item.explanation}
              refs={item.evidence_refs}
            />
          ))}
          <Typography.Title level={5}>{OPPORTUNITY_FIT_COPY.drawer.fitSignals}</Typography.Title>
          {review.triage.fit_signals.map((item) => (
            <ReviewItem key={item.id} statement={item.statement} refs={item.evidence_refs} />
          ))}
          <Typography.Title level={5}>{OPPORTUNITY_FIT_COPY.drawer.gaps}</Typography.Title>
          {review.triage.gaps.map((item) => (
            <ReviewItem
              key={item.id}
              title={`${opportunityFitGapKindLabel(item.kind)} · ${opportunityFitStatusLabel(item.candidate_status)}`}
              statement={item.requirement}
              refs={item.evidence_refs}
            />
          ))}
          {review.triage.next_questions.map((question) => (
            <Typography.Paragraph key={question}>？ {question}</Typography.Paragraph>
          ))}
          <Typography.Title level={5}>{OPPORTUNITY_FIT_COPY.drawer.nextQuestions}</Typography.Title>
          <Typography.Paragraph>
            {review.triage.deadline.status === 'stated' ? review.triage.deadline.text : OPPORTUNITY_FIT_COPY.drawer.notStated}
          </Typography.Paragraph>
          <EvidenceRefs refs={review.triage.deadline.evidence_refs} />

          <Divider />
          <Typography.Title level={5}>{OPPORTUNITY_FIT_COPY.drawer.evidenceSources}</Typography.Title>
          <Card size="small">
            <Typography.Text>{OPPORTUNITY_FIT_COPY.evidence.resume}：{review.source.resume.title}</Typography.Text>
            <br />
            <Typography.Text>{OPPORTUNITY_FIT_COPY.evidence.jd}：{review.source.jd.source_label}</Typography.Text>
            <Typography.Paragraph type="secondary">{OPPORTUNITY_FIT_COPY.drawer.jdOriginal}</Typography.Paragraph>
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{review.source.jd.text}</Typography.Paragraph>
            {review.source.candidate_assertions.length > 0 ? (
              <>
                <Typography.Paragraph strong>{OPPORTUNITY_FIT_COPY.drawer.candidateAssertions}</Typography.Paragraph>
                {review.source.candidate_assertions.map((assertion) => (
                  <Typography.Paragraph key={assertion.index}>· {assertion.text}</Typography.Paragraph>
                ))}
              </>
            ) : null}
          </Card>

          {review.deep_review ? (
            <>
              <Typography.Title level={4}>{OPPORTUNITY_FIT_COPY.drawer.deepReview}</Typography.Title>
              <Typography.Paragraph>{OPPORTUNITY_FIT_COPY.drawer.recommendedPath}：{opportunityFitRecommendedPathLabel(review.deep_review.recommended_path)}</Typography.Paragraph>
              {review.deep_review.strengths.map((item) => (
                <ReviewItem key={item.id} statement={item.statement} refs={item.evidence_refs} />
              ))}
              {review.deep_review.gaps_to_address.map((item) => (
                <ReviewItem key={item.id} statement={item.statement} refs={item.evidence_refs} />
              ))}
              {review.deep_review.questions_to_clarify.map((item) => (
                <ReviewItem key={item.id} statement={item.statement} refs={item.evidence_refs} />
              ))}
              <Typography.Title level={5}>{OPPORTUNITY_FIT_COPY.drawer.nextActions}</Typography.Title>
              {review.deep_review.next_actions.map((action) => (
                <Card size="small" key={action.id} style={{ marginBottom: 8 }}>
                  <Typography.Text>{action.label}</Typography.Text>
                </Card>
              ))}
            </>
          ) : null}
        </div>
      ) : (
        <Spin />
      )}
      </div>
    </Drawer>
  );
}
