import { useEffect, useMemo, useRef, useState } from 'react';
import dayjs from 'dayjs';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Form,
  Input,
  Modal,
  Progress,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  App as AntApp,
} from 'antd';
import { ArrowLeftOutlined, CopyOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import type { Application } from '@/types/application';
import type {
  MaterialKitChecklistItem,
  MaterialKitContent,
  MaterialKitMessage,
  MaterialKitStatus,
  EditableMaterialKitStatus,
  MaterialKitViewModel,
} from '@/types/materialKit';
import type { Resume } from '@/types/resume';
import type { MaterialRevisionProposal } from '@/types/materialRevisionProposal';
import type {
  ConfirmEvidenceBundleInput,
  EvidenceBundleDetail,
  EvidenceBundlePreview,
  EvidenceBundleSummary,
} from '@/types/evidenceBundle';
import {
  confirmEvidenceBundle,
  getEvidenceBundle,
  getEvidenceBundlePreview,
  listEvidenceBundles,
} from '@/services/evidenceBundles';
import {
  generateApplicationMaterialKit,
  getApplicationMaterialKit,
  updateMaterialKit,
} from '@/services/materialKits';
import { listResumes } from '@/services/resumes';
import { createMaterialRevisionProposal } from '@/services/materialRevisionProposals';
import MaterialProposalReviewModal, {
  type MaterialProposalOwnerOperationState,
} from './MaterialProposalReviewModal';
import { ConfirmationPanel } from './ui/ConfirmationPanel';
import { SourceStateTag } from './ui/SourceStateTag';
import styles from './MaterialKitDrawer.module.css';
import { getMaterialKitStatusForSave } from './materialKitStatus';
import { projectMaterialKitSurface } from '@/features/materialSurfaces/materialKitSurface';
import { formatResumeLineage, resumeDisplayTitle } from '@/features/materialSurfaces/materialLabels';
import { resolveResumeLineage } from '@/features/materialSurfaces/resumeLineage';
import {
  materialKitOwnerStore,
  type MaterialKitOwnerDraft,
  type MaterialKitOwnerLease,
} from '@/features/materialSurfaces/materialKitOwnerStore';
import {
  isMaterialFlowSourceConflict,
  MATERIAL_FLOW_COPY,
  materialConfirmationKindLabel,
  materialEvidencePreviewIssueLabels,
  materialFlowErrorMessage,
  type MaterialFlowErrorContext,
} from './materialFlowCopy';

export interface MaterialKitOwnerState {
  pending: boolean;
  resultUnknown: boolean;
  sourceConflict: boolean;
}

interface Props {
  application: Application | null;
  open: boolean;
  onClose: () => void;
  initialResumeID?: number;
  initialJdSnapshot?: string;
  initialJdVersionID?: number;
  /** Owner/controller state may outlive this view while a write is pending. */
  pendingState?: 'none' | 'pending' | 'unknown' | 'result_unknown';
  resultUnknown?: boolean;
  sourceConflict?: boolean;
  /** Reports owner-local uncertainty without moving service writes into the host. */
  onOwnerStateChange?: (state: MaterialKitOwnerState) => void;
}

interface GenerateVariables {
  applicationID: number;
  generation: number;
  resumeID: number;
  jdVersionID: number;
  overwrite: boolean;
}

interface SaveVariables {
  applicationID: number;
  generation: number;
  kitID: number;
  resumeID: number | undefined;
  jdSnapshot: string;
  status: EditableMaterialKitStatus | undefined;
  content: MaterialKitContent;
}

interface ConfirmVariables {
  applicationID: number;
  generation: number;
  sessionID: string;
  input: ConfirmEvidenceBundleInput;
}

interface ProposalVariables {
  applicationID: number;
  generation: number;
  instructions: string;
  userAssertions: string[];
}

const STATUS_LABELS: Record<MaterialKitStatus, string> = {
  draft: '草稿',
  ready: '已准备',
  submitted: '已投递',
};

const EDITABLE_STATUS_OPTIONS: Array<{ label: string; value: EditableMaterialKitStatus }> = [
  { label: STATUS_LABELS.draft, value: 'draft' },
  { label: STATUS_LABELS.ready, value: 'ready' },
];

function createDefaultContent(): MaterialKitContent {
  return {
    resume_advice: {
      summary: '',
      highlights: [],
      rewrite_bullets: [],
      gaps: [],
      notes: '',
    },
    messages: [
      { type: 'recruiter_email', title: 'HR 邮件', body: '', notes: '' },
      { type: 'referral_message', title: '内推私信', body: '', notes: '' },
      { type: 'application_note', title: '投递备注', body: '', notes: '' },
    ],
    checklist: [
      { id: 'confirm_jd', label: '确认岗位 JD 和投递入口', done: false },
      { id: 'select_resume', label: '选择本次实际使用的简历版本', done: false },
      { id: 'tailor_resume', label: '按岗位关键词调整简历', done: false },
      { id: 'prepare_message', label: '准备沟通话术和备注', done: false },
      { id: 'submit_application', label: '完成投递', done: false },
      { id: 'set_followup', label: '设置跟进提醒', done: false },
    ],
  };
}

function cloneContent(content: MaterialKitContent): MaterialKitContent {
  return {
    resume_advice: {
      summary: content.resume_advice.summary || '',
      highlights: [...(content.resume_advice.highlights || [])],
      rewrite_bullets: [...(content.resume_advice.rewrite_bullets || [])],
      gaps: [...(content.resume_advice.gaps || [])],
      notes: content.resume_advice.notes || '',
    },
    messages: (content.messages || []).map((message) => ({ ...message })),
    checklist: (content.checklist || []).map((item) => ({ ...item })),
  };
}

function textToLines(value: string): string[] {
  return value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

function linesToText(value: string[]): string {
  return value.join('\n');
}

function toLocalDateTimeInputValue(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0');

  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
  ].join('-') + `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatEvidenceTimestamp(value: string): string {
  return dayjs(value).format('YYYY-MM-DD HH:mm');
}

function formatConfirmationKind(value: string): string {
  return materialConfirmationKindLabel(value);
}

function getErrorMessage(error: unknown, context: MaterialFlowErrorContext = 'general'): string {
  return materialFlowErrorMessage(error, context);
}

interface ProposalAssertionsValidation {
  values: string[];
  error: string | null;
}

function validateProposalAssertions(raw: string): ProposalAssertionsValidation {
  const values = raw
    .split(/\r?\n/)
    .map((assertion) => assertion.trim())
    .filter(Boolean);
  if (values.length > 10) {
    return { values, error: MATERIAL_FLOW_COPY.drawer.proposalValidationTooMany };
  }
  if (values.some((assertion) => assertion.length > 500)) {
    return { values, error: MATERIAL_FLOW_COPY.drawer.proposalValidationTooLong };
  }
  return { values, error: null };
}

function isValidPositiveId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isValidApplication(application: Application | null): application is Application {
  try {
    return application !== null
      && isValidPositiveId(application.id)
      && !hasDeletionMarker(application);
  } catch {
    return false;
  }
}

function hasDeletionMarker(value: unknown): boolean {
  try {
    if (!value || typeof value !== 'object') return false;
    const row = value as Record<string, unknown>;
    return row.deleted === true
      || row.deleted_at != null
      || row.deletedAt != null;
  } catch {
    return true;
  }
}

function hasStaleMarker(value: unknown): boolean {
  try {
    return isRecord(value)
      && (value.stale === true || value.is_stale === true);
  } catch {
    return true;
  }
}

function isValidMaterialKitContent(value: unknown): boolean {
  try {
    if (!value || typeof value !== 'object') return false;
    const content = value as Record<string, unknown>;
    const advice = content.resume_advice;
    if (!advice || typeof advice !== 'object') return false;
    const adviceRecord = advice as Record<string, unknown>;
    return typeof adviceRecord.summary === 'string'
      && Array.isArray(adviceRecord.highlights)
      && adviceRecord.highlights.every((item) => typeof item === 'string')
      && Array.isArray(adviceRecord.rewrite_bullets)
      && adviceRecord.rewrite_bullets.every((item) => typeof item === 'string')
      && Array.isArray(adviceRecord.gaps)
      && adviceRecord.gaps.every((item) => typeof item === 'string')
      && typeof adviceRecord.notes === 'string'
      && Array.isArray(content.messages)
      && content.messages.every((item) => {
        if (!item || typeof item !== 'object') return false;
        const message = item as Record<string, unknown>;
        return typeof message.type === 'string'
          && typeof message.title === 'string'
          && typeof message.body === 'string'
          && typeof message.notes === 'string';
      })
      && Array.isArray(content.checklist)
      && content.checklist.every((item) => {
        if (!item || typeof item !== 'object') return false;
        const checklistItem = item as Record<string, unknown>;
        return typeof checklistItem.id === 'string'
          && typeof checklistItem.label === 'string'
          && typeof checklistItem.done === 'boolean';
      });
  } catch {
    return false;
  }
}

function isValidMaterialKitForApplication(value: unknown, applicationID: number): value is MaterialKitViewModel {
  try {
    if (!isValidPositiveId(applicationID) || !value || typeof value !== 'object') return false;
    const kit = value as Partial<MaterialKitViewModel> & Record<string, unknown>;
    return isValidPositiveId(kit.id)
      && isValidPositiveId(kit.application_id)
      && kit.application_id === applicationID
      && (kit.status === 'draft' || kit.status === 'ready' || kit.status === 'submitted')
      && typeof kit.jd_snapshot === 'string'
      && typeof kit.updated_at === 'string'
      && typeof kit.created_at === 'string'
      && isValidMaterialKitContent(kit.content)
      && (!('resume_id' in kit) || kit.resume_id === undefined || isValidPositiveId(kit.resume_id))
      && (!('jd_version_id' in kit) || kit.jd_version_id === undefined || isValidPositiveId(kit.jd_version_id))
      && !hasDeletionMarker(kit)
      && !hasStaleMarker(kit);
  } catch {
    return false;
  }
}

function isValidProposalForApplication(value: unknown, applicationID: number): value is MaterialRevisionProposal {
  try {
    if (!isValidPositiveId(applicationID) || !value || typeof value !== 'object') return false;
    const proposal = value as Partial<MaterialRevisionProposal> & Record<string, unknown>;
    const source = proposal.source;
    if (!source || typeof source !== 'object') return false;
    const sourceRecord = source as Record<string, unknown>;
    const sourceApplication = sourceRecord.application;
    const sourceKit = sourceRecord.material_kit;
    const sourceResume = sourceRecord.resume;
    return isValidPositiveId(proposal.id)
      && proposal.application_id === applicationID
      && isValidPositiveId(proposal.material_kit_id)
      && typeof proposal.proposal_sha256 === 'string'
      && proposal.proposal_sha256.length > 0
      && proposal.status === 'draft'
      && !!sourceApplication
      && typeof sourceApplication === 'object'
      && !hasDeletionMarker(sourceApplication)
      && (sourceApplication as Record<string, unknown>).id === applicationID
      && !!sourceKit
      && typeof sourceKit === 'object'
      && !hasDeletionMarker(sourceKit)
      && isValidPositiveId((sourceKit as Record<string, unknown>).id)
      && (sourceKit as Record<string, unknown>).id === proposal.material_kit_id
      && !!sourceResume
      && typeof sourceResume === 'object'
      && !hasDeletionMarker(sourceResume)
      && isValidPositiveId((sourceResume as Record<string, unknown>).id)
      && (sourceResume as Record<string, unknown>).id === proposal.source_resume_id
      && Array.isArray(proposal.changes)
      && Array.isArray(proposal.accepted_change_ids)
      && Array.isArray((sourceRecord as Record<string, unknown>).user_assertions);
  } catch {
    return false;
  }
}

function isValidEvidencePreviewForApplication(value: unknown, applicationID: number): value is EvidenceBundlePreview {
  try {
    if (!isValidPositiveId(applicationID) || !value || typeof value !== 'object') return false;
    const preview = value as Record<string, unknown>;
    const sources = preview.sources;
    const sourceRecord = isRecord(sources) ? sources : null;
    const sourceApplication = sourceRecord && isRecord(sourceRecord.application) ? sourceRecord.application : null;
    const sourceJd = sourceRecord && isRecord(sourceRecord.jd) ? sourceRecord.jd : null;
    const sourceResume = sourceRecord && isRecord(sourceRecord.resume) ? sourceRecord.resume : null;
    const sourceKit = sourceRecord && isRecord(sourceRecord.material_kit) ? sourceRecord.material_kit : null;
    const readySourcesValid = !preview.ready || (
      typeof preview.bundle_sha256 === 'string'
      && preview.bundle_sha256.length > 0
      && !!sourceApplication
      && sourceApplication.id === applicationID
      && !!sourceJd
      && typeof sourceJd.sha256 === 'string'
      && sourceJd.sha256.length > 0
      && Number.isSafeInteger(sourceJd.characters)
      && (sourceJd.characters as number) >= 0
      && !!sourceResume
      && isValidPositiveId(sourceResume.id)
      && typeof sourceResume.sha256 === 'string'
      && sourceResume.sha256.length > 0
      && !!sourceKit
      && isValidPositiveId(sourceKit.id)
      && typeof sourceKit.sha256 === 'string'
      && sourceKit.sha256.length > 0
    );
    const issues = preview.issues;
    return preview.application_id === applicationID
      && typeof preview.ready === 'boolean'
      && Array.isArray(issues)
      && issues.every((issue) => typeof issue === 'string')
      && readySourcesValid;
  } catch {
    return false;
  }
}

function isValidEvidenceDetailForApplication(
  value: unknown,
  applicationID: number,
  bundleID: number,
): value is EvidenceBundleDetail {
  try {
    if (!isValidPositiveId(applicationID) || !isValidPositiveId(bundleID) || !value || typeof value !== 'object') return false;
    const detail = value as Partial<EvidenceBundleDetail> & Record<string, unknown>;
    return detail.id === bundleID
      && detail.application_id === applicationID
      && isValidPositiveId(detail.sequence)
      && typeof detail.submitted_at === 'string'
      && typeof detail.confirmed_at === 'string'
      && typeof detail.confirmation_kind === 'string'
      && typeof detail.bundle_sha256 === 'string'
      && detail.bundle_sha256.length > 0
      && typeof detail.created_at === 'string'
      && !!detail.snapshot
      && typeof detail.snapshot === 'object';
  } catch {
    return false;
  }
}

export default function MaterialKitDrawer({
  application,
  open,
  onClose,
  initialResumeID,
  initialJdSnapshot,
  initialJdVersionID,
  pendingState: externalPendingState,
  resultUnknown: externalResultUnknown = false,
  sourceConflict: externalSourceConflict = false,
  onOwnerStateChange,
}: Props) {
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const applicationID = application?.id;
  const activeApplicationIDRef = useRef<number | undefined>(applicationID);
  const blockedPreviewUpdatedAtRef = useRef<number | null>(null);
  const ownerLeaseRef = useRef<MaterialKitOwnerLease | null>(null);
  const ownerDraftActiveRef = useRef(false);
  const ownerSkipDraftPersistGenerationRef = useRef<number | null>(null);
  const ownerSkipConfirmationPersistGenerationRef = useRef<number | null>(null);

  // Acquiring a lease mutates the shared owner store and must only happen
  // after commit.  Render-time acquisition breaks React StrictMode purity and
  // can create a generation that never corresponds to a mounted owner.
  const [ownerLeaseRevision, setOwnerLeaseRevision] = useState(0);
  const ownerLease = ownerLeaseRef.current;
  const initialOwnerSnapshot = ownerLease?.read();
  const initialOwnerDraft = initialOwnerSnapshot?.draft?.hasLocalState ? initialOwnerSnapshot.draft : null;
  const initialOwnerConfirmation = initialOwnerSnapshot?.confirmation;

  useEffect(() => {
    const previousLease = ownerLeaseRef.current;
    previousLease?.release();
    const nextLease = isValidPositiveId(applicationID)
      ? materialKitOwnerStore.acquire(applicationID)
      : null;
    ownerLeaseRef.current = nextLease;
    ownerSkipDraftPersistGenerationRef.current = nextLease?.generation ?? null;
    ownerSkipConfirmationPersistGenerationRef.current = nextLease?.generation ?? null;
    ownerDraftActiveRef.current = Boolean(nextLease?.read().draft?.hasLocalState);
    setOwnerLeaseRevision((revision) => revision + 1);

    return () => {
      if (ownerLeaseRef.current === nextLease) nextLease?.release();
    };
  }, [applicationID]);

  const confirmationSessionRef = useRef<string | null>(initialOwnerConfirmation?.key ?? null);
  activeApplicationIDRef.current = applicationID;

  const [existingKit, setExistingKit] = useState<MaterialKitViewModel | null>(null);
  const [resumeID, setResumeID] = useState<number | undefined>(() => initialOwnerDraft?.resumeID);
  const [jdSnapshot, setJdSnapshotState] = useState(() => initialOwnerDraft?.jdSnapshot || '');
  const [jdVersionID, setJdVersionID] = useState<number | undefined>(() => initialOwnerDraft?.jdVersionID ?? initialJdVersionID);
  const setJdSnapshot = (value: string) => {
    if (!jdVersionID) {
      ownerDraftActiveRef.current = true;
      setJdSnapshotState(value);
    }
  };
  const [status, setStatus] = useState<EditableMaterialKitStatus>(() => initialOwnerDraft?.status ?? 'draft');
  const [content, setContent] = useState<MaterialKitContent>(() => initialOwnerDraft ? cloneContent(initialOwnerDraft.content) : createDefaultContent());
  const [draftDirty, setDraftDirty] = useState(() => initialOwnerDraft?.draftDirty ?? false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmationOpen, setConfirmationOpen] = useState(() => initialOwnerConfirmation?.open ?? false);
  const [confirmationKey, setConfirmationKey] = useState<string | null>(() => initialOwnerConfirmation?.key ?? null);
  const [confirmationSubmittedAt, setConfirmationSubmittedAt] = useState(() => initialOwnerConfirmation?.submittedAt ?? '');
  const [confirmationError, setConfirmationError] = useState<string | null>(() => initialOwnerConfirmation?.error ?? null);
  const [confirmationRefreshing, setConfirmationRefreshing] = useState(false);
  const [confirmationPreviewValid, setConfirmationPreviewValid] = useState(() => initialOwnerConfirmation?.previewValid ?? true);
  const [confirmationResultUnknown, setConfirmationResultUnknown] = useState(() => initialOwnerConfirmation?.resultUnknown ?? false);
  const [confirmationSourceConflict, setConfirmationSourceConflict] = useState(() => initialOwnerConfirmation?.sourceConflict ?? false);
  const previousInternalOwnerStateKeyRef = useRef<string | null>(null);
  const [evidenceDetailOpen, setEvidenceDetailOpen] = useState(false);
  const [evidenceDetail, setEvidenceDetail] = useState<EvidenceBundleDetail | null>(null);
  const [evidenceDetailError, setEvidenceDetailError] = useState<string | null>(null);
  const [evidenceDetailLoading, setEvidenceDetailLoading] = useState(false);
  const [proposalReviewOpen, setProposalReviewOpen] = useState(false);
  const [proposal, setProposal] = useState<MaterialRevisionProposal | null>(null);
  const [proposalAssertions, setProposalAssertions] = useState(() => initialOwnerDraft?.proposalAssertions ?? '');
  const proposalAssertionsValidation = useMemo(
    () => validateProposalAssertions(proposalAssertions),
    [proposalAssertions],
  );
  const markOwnerDraftDirty = () => {
    ownerDraftActiveRef.current = true;
    setDraftDirty(true);
  };

  const isCurrentOwnerRequest = (requestedApplicationID: number, generation: number): boolean => {
    const currentLease = ownerLeaseRef.current;
    return activeApplicationIDRef.current === requestedApplicationID
      && currentLease?.applicationId === requestedApplicationID
      && currentLease?.generation === generation
      && currentLease.isCurrent();
  };

  const isCurrentConfirmationSession = (
    requestedApplicationID: number,
    sessionID: string,
    generation: number,
  ) => isCurrentOwnerRequest(requestedApplicationID, generation)
    && confirmationSessionRef.current === sessionID;

  const resetEditor = (nextApplication: Application | null) => {
    const ownerSnapshot = ownerLease?.read();
    const storedDraft = ownerSnapshot?.draft?.hasLocalState ? ownerSnapshot.draft : null;
    const storedConfirmation = ownerSnapshot?.confirmation;
    // A JD text is also supplied as the read-only context in ordinary drawer
    // renders.  Only a scoped resume handoff is an owner draft that must be
    // retained before the first edit.
    const hasInitialPrefill = initialResumeID !== undefined;

    ownerDraftActiveRef.current = Boolean(storedDraft || (ownerLease && hasInitialPrefill));
    setExistingKit(null);
    setResumeID(storedDraft?.resumeID ?? initialResumeID);
    setJdSnapshotState(storedDraft?.jdSnapshot ?? ((initialJdSnapshot ?? nextApplication?.notes) || ''));
    setJdVersionID(storedDraft?.jdVersionID ?? initialJdVersionID);
    setStatus(storedDraft?.status ?? 'draft');
    setContent(storedDraft ? cloneContent(storedDraft.content) : createDefaultContent());
    setDraftDirty(storedDraft?.draftDirty ?? false);
    setActionError(null);
    const confirmationUnresolved = Boolean(
      storedConfirmation?.pending || storedConfirmation?.resultUnknown || storedConfirmation?.sourceConflict,
    );
    setConfirmationOpen(Boolean(storedConfirmation?.open || confirmationUnresolved));
    setConfirmationKey(storedConfirmation?.key ?? null);
    setConfirmationSubmittedAt(storedConfirmation?.submittedAt ?? '');
    setConfirmationError(storedConfirmation?.error ?? null);
    setConfirmationRefreshing(false);
    setConfirmationPreviewValid(storedConfirmation?.previewValid ?? true);
    setConfirmationResultUnknown(storedConfirmation?.resultUnknown ?? false);
    setConfirmationSourceConflict(storedConfirmation?.sourceConflict ?? false);
    setEvidenceDetailOpen(false);
    setEvidenceDetail(null);
    setEvidenceDetailError(null);
    setEvidenceDetailLoading(false);
    setProposalReviewOpen(false);
    setProposal(null);
    setProposalAssertions(storedDraft?.proposalAssertions ?? '');
    confirmationSessionRef.current = storedConfirmation?.key ?? null;
    blockedPreviewUpdatedAtRef.current = null;

    // A handoff can provide a local starting point before a server kit exists.
    // Persist that prefill immediately so a close/reopen does not lose it.
    if (!storedDraft && ownerLease && hasInitialPrefill) {
      ownerLease.writeDraft({
        hasLocalState: true,
        resumeID: initialResumeID,
        jdSnapshot: (initialJdSnapshot ?? nextApplication?.notes) || '',
        jdVersionID: initialJdVersionID,
        status: 'draft',
        content: createDefaultContent(),
        proposalAssertions: '',
        draftDirty: false,
      });
    }
  };

  const applyKitToEditor = (kit: MaterialKitViewModel, preserveLocalDraft = true) => {
    setExistingKit(kit);
    if (preserveLocalDraft && ownerDraftActiveRef.current) {
      setActionError(null);
      return;
    }
    ownerDraftActiveRef.current = false;
    ownerLease?.writeDraft(null);
    setResumeID(kit.resume_id);
    setJdSnapshotState(kit.jd_snapshot);
    setJdVersionID(kit.jd_version_id);
    setStatus(kit.status === 'submitted' ? 'draft' : kit.status);
    setContent(cloneContent(kit.content));
    setDraftDirty(false);
    setActionError(null);
  };

  const kitQuery = useQuery({
    queryKey: ['application-material-kit', applicationID],
    queryFn: () => getApplicationMaterialKit(applicationID!),
    enabled: open && isValidPositiveId(applicationID),
  });

  const resumesQuery = useQuery({
    queryKey: ['resumes'],
    queryFn: () => listResumes(),
    enabled: open,
  });

  const evidencePreviewQuery = useQuery<EvidenceBundlePreview>({
    queryKey: ['application-evidence-bundle-preview', applicationID],
    queryFn: () => getEvidenceBundlePreview(applicationID!),
    enabled: open && isValidPositiveId(applicationID),
  });

  const evidenceHistoryQuery = useQuery({
    queryKey: ['application-evidence-bundles', applicationID],
    queryFn: () => listEvidenceBundles(applicationID!),
    enabled: open && isValidPositiveId(applicationID),
  });

  useEffect(() => {
    resetEditor(open ? application : null);
  }, [applicationID, initialResumeID, open, ownerLeaseRevision]);

  useEffect(() => {
    if (!ownerLease?.isCurrent()) return;
    if (evidencePreviewQuery.isError) {
      setConfirmationPreviewValid(false);
      return;
    }

    if (!evidencePreviewQuery.isSuccess
      || !isValidEvidencePreviewForApplication(evidencePreviewQuery.data, applicationID ?? 0)
      || !evidencePreviewQuery.data.ready
      || confirmationRefreshing) return;

    if (
      blockedPreviewUpdatedAtRef.current !== null
      && evidencePreviewQuery.dataUpdatedAt <= blockedPreviewUpdatedAtRef.current
    ) {
      return;
    }

    if (
      confirmationOpen
      && (!applicationID || !confirmationKey || !isCurrentConfirmationSession(applicationID, confirmationKey, ownerLease.generation))
    ) {
      return;
    }

    setConfirmationPreviewValid(true);
    blockedPreviewUpdatedAtRef.current = null;
  }, [
    applicationID,
    confirmationKey,
    confirmationOpen,
    confirmationRefreshing,
    evidencePreviewQuery.data,
    evidencePreviewQuery.dataUpdatedAt,
    evidencePreviewQuery.isError,
    evidencePreviewQuery.isSuccess,
    ownerLease,
  ]);

  useEffect(() => {
    if (!open || !isValidApplication(application) || !ownerLease?.isCurrent() || !kitQuery.isSuccess) return;

    const kit = kitQuery.data;
    if (!kit) {
      resetEditor(application);
      return;
    }

    if (!isValidMaterialKitForApplication(kit, application.id)) {
      setExistingKit(null);
      setActionError('材料包来源无效，已停止写入');
      return;
    }
    applyKitToEditor(kit);
  }, [application, applicationID, kitQuery.data, kitQuery.isSuccess, open, ownerLease]);

  useEffect(() => {
    if (!kitQuery.isError || !ownerLease?.isCurrent()) return;

    if (ownerDraftActiveRef.current) {
      setActionError(getErrorMessage(kitQuery.error));
      return;
    }
    // Preserve an already loaded kit during a transient refetch error.  A
    // related JD query can move from loading to ready independently, and that
    // transition must never erase the editable kit in this owner surface.
    if (existingKit) {
      setActionError(getErrorMessage(kitQuery.error));
      return;
    }
    setExistingKit(null);
    setResumeID(undefined);
    setJdSnapshotState(application?.notes || '');
    setJdVersionID(undefined);
    setStatus('draft');
    setContent(createDefaultContent());
    setDraftDirty(false);
    setActionError(getErrorMessage(kitQuery.error));
  }, [application?.notes, existingKit, kitQuery.error, kitQuery.isError, ownerLease]);

  useEffect(() => {
    // JD data is allowed to arrive after the kit.  Only hydrate an empty
    // editor; never reset a loaded kit or a user-owned draft on that update.
    if (!open || existingKit || ownerDraftActiveRef.current || !ownerLease?.isCurrent()) return;
    if (initialJdSnapshot !== undefined && !jdSnapshot) setJdSnapshotState(initialJdSnapshot);
    if (initialJdVersionID !== undefined && jdVersionID === undefined) setJdVersionID(initialJdVersionID);
  }, [existingKit, initialJdSnapshot, initialJdVersionID, jdSnapshot, jdVersionID, open, ownerLease]);

  const completion = useMemo(() => {
    const checklist = content.checklist || [];
    if (checklist.length === 0) return 0;
    const done = checklist.filter((item) => item.done).length;
    return Math.round((done / checklist.length) * 100);
  }, [content.checklist]);

  const generateMutation = useMutation({
    mutationFn: ({ applicationID: requestedApplicationID, resumeID: requestedResumeID, jdVersionID: requestedJdVersionID, overwrite }: GenerateVariables) =>
      generateApplicationMaterialKit(requestedApplicationID, {
        resume_id: requestedResumeID,
        jd_version_id: requestedJdVersionID,
        overwrite,
      }),
    onSuccess: (kit, variables) => {
      if (!isCurrentOwnerRequest(variables.applicationID, variables.generation)) return;
      if (!isValidMaterialKitForApplication(kit, variables.applicationID)) {
        setActionError('材料包响应无效，已停止写入');
        return;
      }
      queryClient.setQueryData(['application-material-kit', variables.applicationID], kit);

      applyKitToEditor(kit, false);
      message.success('材料包已生成');
    },
    onError: (error, variables) => {
      if (isCurrentOwnerRequest(variables.applicationID, variables.generation)) {
        setActionError(getErrorMessage(error));
      }
    },
  });

  const saveMutation = useMutation({
    mutationFn: ({ kitID, resumeID: requestedResumeID, jdSnapshot, status, content }: SaveVariables) =>
      updateMaterialKit(kitID, {
        resume_id: requestedResumeID,
        jd_snapshot: jdSnapshot,
        status,
        content_json: content,
      }),
    onSuccess: (kit, variables) => {
      if (!isCurrentOwnerRequest(variables.applicationID, variables.generation)) return;
      if (!isValidMaterialKitForApplication(kit, variables.applicationID)) {
        setActionError('材料包响应无效，已停止写入');
        return;
      }
      queryClient.setQueryData(['application-material-kit', variables.applicationID], kit);

      applyKitToEditor(kit, false);
      message.success('材料包已保存');
    },
    onError: (error, variables) => {
      if (isCurrentOwnerRequest(variables.applicationID, variables.generation)) {
        setActionError(getErrorMessage(error));
      }
    },
  });

  const proposalMutation = useMutation({
    mutationFn: ({ applicationID: requestedApplicationID, instructions, userAssertions }: ProposalVariables) =>
      createMaterialRevisionProposal(requestedApplicationID, {
        instructions,
        user_assertions: userAssertions,
      }),
    onSuccess: (nextProposal: MaterialRevisionProposal, variables: ProposalVariables) => {
      if (!isCurrentOwnerRequest(variables.applicationID, variables.generation)) return;
      if (!isValidProposalForApplication(nextProposal, variables.applicationID)
        || !existingKit
        || nextProposal.material_kit_id !== existingKit.id
        || nextProposal.source.material_kit.id !== existingKit.id) {
        setActionError('提案来源无效，已停止写入');
        return;
      }
      setProposal(nextProposal);
      setProposalReviewOpen(true);
    },
    onError: (error: unknown, variables: ProposalVariables) => {
      if (isCurrentOwnerRequest(variables.applicationID, variables.generation)) {
        setActionError(getErrorMessage(error, 'proposal'));
      }
    },
  });

  const refreshEvidencePreview = async (
    requestedApplicationID: number,
    sessionID: string,
    generation: number,
  ) => {
    if (!isCurrentConfirmationSession(requestedApplicationID, sessionID, generation)) return;

    setConfirmationRefreshing(true);
    setConfirmationPreviewValid(false);
    try {
      const result = await evidencePreviewQuery.refetch();
      if (!isCurrentConfirmationSession(requestedApplicationID, sessionID, generation)) return;

      if (result.isSuccess
        && isValidEvidencePreviewForApplication(result.data, requestedApplicationID)
        && result.data.ready) {
        setConfirmationPreviewValid(true);
        setConfirmationResultUnknown(false);
        setConfirmationSourceConflict(false);
        ownerLeaseRef.current?.patchConfirmation({ pending: false, resultUnknown: false, sourceConflict: false });
        blockedPreviewUpdatedAtRef.current = null;
        return;
      }

      setConfirmationError('材料证据刷新失败，请重试刷新后再确认');
    } catch {
      if (isCurrentConfirmationSession(requestedApplicationID, sessionID, generation)) {
        setConfirmationError('材料证据刷新失败，请重试刷新后再确认');
      }
    } finally {
      if (isCurrentConfirmationSession(requestedApplicationID, sessionID, generation)) {
        setConfirmationRefreshing(false);
      }
    }
  };

  const confirmMutation = useMutation({
    mutationFn: ({ applicationID: requestedApplicationID, input }: ConfirmVariables) =>
      confirmEvidenceBundle(requestedApplicationID, input),
    onSuccess: (_bundle, variables) => {
      if (!isCurrentConfirmationSession(variables.applicationID, variables.sessionID, variables.generation)) return;
      queryClient.invalidateQueries({ queryKey: ['application-evidence-bundle-preview', variables.applicationID] });
      queryClient.invalidateQueries({ queryKey: ['application-evidence-bundles', variables.applicationID] });
      queryClient.invalidateQueries({ queryKey: ['events'] });
      queryClient.invalidateQueries({ queryKey: ['events', variables.applicationID] });
      queryClient.invalidateQueries({ queryKey: ['applications'] });
      setConfirmationOpen(false);
      setConfirmationKey(null);
      setConfirmationError(null);
      setConfirmationRefreshing(false);
      setConfirmationPreviewValid(true);
      setConfirmationResultUnknown(false);
      setConfirmationSourceConflict(false);
      confirmationSessionRef.current = null;
      blockedPreviewUpdatedAtRef.current = null;
      ownerLeaseRef.current?.writeConfirmation(null);
      message.success('本次投递记录已保存');
    },
    onError: (error: unknown, variables) => {
      if (!isCurrentConfirmationSession(variables.applicationID, variables.sessionID, variables.generation)) return;

      if (isMaterialFlowSourceConflict(error)) {
        setConfirmationError(getErrorMessage(error, 'confirmation'));
        setConfirmationPreviewValid(false);
        setConfirmationResultUnknown(false);
        setConfirmationSourceConflict(true);
        blockedPreviewUpdatedAtRef.current = evidencePreviewQuery.dataUpdatedAt;
        ownerLeaseRef.current?.patchConfirmation({
          open: true,
          pending: false,
          resultUnknown: false,
          sourceConflict: true,
        });
        void refreshEvidencePreview(variables.applicationID, variables.sessionID, variables.generation);
        return;
      }

      setConfirmationResultUnknown(true);
      setConfirmationError(getErrorMessage(error, 'confirmation'));
      ownerLeaseRef.current?.patchConfirmation({
        open: true,
        pending: false,
        resultUnknown: true,
        sourceConflict: false,
      });
    },
  });

  useEffect(() => {
    if (!ownerLease) return;
    if (ownerSkipDraftPersistGenerationRef.current === ownerLease.generation) {
      ownerSkipDraftPersistGenerationRef.current = null;
      return;
    }
    if (!ownerDraftActiveRef.current) return;
    const draft: MaterialKitOwnerDraft = {
      hasLocalState: true,
      resumeID,
      jdSnapshot,
      jdVersionID,
      status,
      content: cloneContent(content),
      proposalAssertions,
      draftDirty,
    };
    ownerLease.writeDraft(draft);
  }, [content, draftDirty, jdSnapshot, jdVersionID, ownerLease, proposalAssertions, resumeID, status]);

  useEffect(() => {
    if (!ownerLease) return;
    if (ownerSkipConfirmationPersistGenerationRef.current === ownerLease.generation) {
      ownerSkipConfirmationPersistGenerationRef.current = null;
      return;
    }

    const stored = ownerLease.read().confirmation;
    const pending = confirmMutation.isPending || stored?.pending === true;
    const unresolved = pending || confirmationResultUnknown || confirmationSourceConflict;
    const key = confirmationKey ?? stored?.key ?? null;
    if (!key && !confirmationOpen && !unresolved) {
      ownerLease.writeConfirmation(null);
      return;
    }
    ownerLease.writeConfirmation({
      open: unresolved ? true : confirmationOpen,
      key,
      submittedAt: confirmationSubmittedAt,
      error: confirmationError,
      previewValid: confirmationPreviewValid,
      pending,
      resultUnknown: confirmationResultUnknown,
      sourceConflict: confirmationSourceConflict,
    });
  }, [confirmationError, confirmationKey, confirmationOpen, confirmationPreviewValid, confirmationRefreshing, confirmationResultUnknown, confirmationSourceConflict, confirmationSubmittedAt, confirmMutation.isPending, ownerLease]);

  const availableResumes: Resume[] = Array.isArray(resumesQuery.data) ? resumesQuery.data : [];
  const resumeOptions = availableResumes.map((resume: Resume) => {
    const lineage = resolveResumeLineage(availableResumes, resume.id);
    const relationshipKnown = lineage.kind !== 'relationship_unknown';
    return {
      label: `${resumeDisplayTitle(resume)} · ${formatResumeLineage(lineage)}`,
      value: resume.id,
      disabled: !relationshipKnown,
    };
  });
  const selectedResumeLineage = resumeID === undefined
    ? null
    : resolveResumeLineage(availableResumes, resumeID);
  const selectedResumeRelationshipKnown = selectedResumeLineage?.kind !== 'relationship_unknown';
  const resumeSelectionBlocked = resumeID !== undefined && !selectedResumeRelationshipKnown;

  const canSave = Boolean(existingKit && applicationID && existingKit.application_id === applicationID && !resumeSelectionBlocked);
  const legacySubmitted = existingKit?.status === 'submitted';
  const canConfirm = Boolean(canSave && !legacySubmitted);
  const displayedStatus: MaterialKitStatus = legacySubmitted ? 'submitted' : status;
  const ownerAvailable = isValidApplication(application) && Boolean(ownerLease?.isCurrent());
  const materialKitSourceInvalid = kitQuery.isError
    || (kitQuery.isSuccess && kitQuery.data !== null && !isValidMaterialKitForApplication(kitQuery.data, applicationID ?? 0));
  const generateDisabled = legacySubmitted || materialKitSourceInvalid || !ownerAvailable || !isValidPositiveId(applicationID) || !isValidPositiveId(resumeID) || !selectedResumeRelationshipKnown || !isValidPositiveId(jdVersionID) || !jdSnapshot.trim();
  const proposalDisabled = legacySubmitted || materialKitSourceInvalid || !ownerAvailable || !isValidPositiveId(applicationID) || !existingKit || existingKit.application_id !== applicationID || !isValidPositiveId(resumeID) || !selectedResumeRelationshipKnown || !isValidPositiveId(jdVersionID) || !jdSnapshot.trim();
  const busy = kitQuery.isFetching || generateMutation.isPending || saveMutation.isPending || proposalMutation.isPending || confirmMutation.isPending || confirmationRefreshing;
  const ownerConfirmation = ownerLease?.read().confirmation;
  const ownerProposal = ownerLease?.read().proposal;

  const internalOwnerState: MaterialKitOwnerState = {
    pending: confirmMutation.isPending || ownerConfirmation?.pending === true,
    resultUnknown: confirmationResultUnknown
      || ownerConfirmation?.resultUnknown === true
      || ownerProposal?.resultUnknown === true,
    sourceConflict: confirmationSourceConflict
      || ownerConfirmation?.sourceConflict === true
      || ownerProposal?.sourceConflict === true,
  };
  internalOwnerState.pending = internalOwnerState.pending || ownerProposal?.pending === true;
  const externalOwnerPending = externalPendingState === 'pending';
  const externalOwnerUnknown = externalResultUnknown
    || externalPendingState === 'unknown'
    || externalPendingState === 'result_unknown';
  const externalOwnerSourceConflict = externalSourceConflict;
  const ownerStateBlocksWrites = externalOwnerPending
    || externalOwnerUnknown
    || externalOwnerSourceConflict
    || internalOwnerState.pending
    || internalOwnerState.resultUnknown
    || internalOwnerState.sourceConflict;
  const editorWritesBlocked = busy || confirmationOpen || ownerStateBlocksWrites || resumeSelectionBlocked || !ownerAvailable || materialKitSourceInvalid;
  const confirmationWriteBlocked = busy || ownerStateBlocksWrites || !ownerAvailable || materialKitSourceInvalid;

  useEffect(() => {
    // The canonical Drawer writes its generation-checked owner lease directly.
    // Report the same bounded state to an optional host bridge as well; the
    // host stores it in the canonical application-scoped store rather than in
    // a second local mirror.  Standalone embeds can use the callback as their
    // only owner bridge.
    const nextKey = `${ownerLease?.generation ?? 0}:${internalOwnerState.pending ? '1' : '0'}${internalOwnerState.resultUnknown ? '1' : '0'}${internalOwnerState.sourceConflict ? '1' : '0'}`;
    const previousKey = previousInternalOwnerStateKeyRef.current;
    previousInternalOwnerStateKeyRef.current = nextKey;

    // A host-provided unresolved state is authoritative.  Never report the
    // drawer's initial empty snapshot back to the host and accidentally clear
    // that state during the post-commit lease acquisition.
    if (externalOwnerPending || externalOwnerUnknown || externalOwnerSourceConflict) return;

    // External owner state is already canonical in the host. Only report
    // local transitions so a persisted external pending/unknown state cannot
    // be cleared merely because this view remounted.
    if (previousKey === null) return;
    if (previousKey !== nextKey) onOwnerStateChange?.(internalOwnerState);
  }, [
    externalOwnerPending,
    externalOwnerSourceConflict,
    externalOwnerUnknown,
    internalOwnerState.pending,
    internalOwnerState.resultUnknown,
    internalOwnerState.sourceConflict,
    ownerLease,
    onOwnerStateChange,
  ]);

  const materialSurface = projectMaterialKitSurface({
    applicationId: applicationID ?? 0,
    jd: applicationID && jdVersionID && jdSnapshot.trim()
      ? { status: 'ready', value: { id: jdVersionID, text: jdSnapshot } }
      : { status: 'absent', value: null },
    resumes: resumesQuery.isFetching && !resumesQuery.data
      ? { status: 'loading', value: null }
      : resumesQuery.isError
        ? { status: 'error', value: null }
        : { status: 'ready', value: resumesQuery.data || [] },
    materialKit: kitQuery.isFetching && !kitQuery.data
      ? { status: 'loading', value: null }
      : kitQuery.isError
        ? { status: 'error', value: null }
        : { status: 'ready', value: kitQuery.data || null },
    selectedResumeId: resumeID,
    draftDirty,
    pendingState: externalOwnerUnknown || internalOwnerState.resultUnknown
      ? 'result_unknown'
      : externalOwnerSourceConflict || internalOwnerState.sourceConflict
        ? 'none'
        : externalOwnerPending || internalOwnerState.pending || confirmationOpen
          ? 'pending'
          : 'none',
    resultUnknown: externalOwnerUnknown || internalOwnerState.resultUnknown,
    sourceConflict: externalOwnerSourceConflict || internalOwnerState.sourceConflict,
  });

  const fallbackSurfaceAction = materialSurface.primaryAction.id === 'open_jd'
    || materialSurface.primaryAction.id === 'select_resume'
    || materialSurface.primaryAction.id === 'resolve';
  const handleSurfaceFallbackAction = () => {
    switch (materialSurface.primaryAction.id) {
      case 'open_jd':
        onClose();
        break;
      case 'select_resume':
        document.getElementById('material-kit-resume-select')?.focus();
        break;
      case 'resolve':
        setActionError('请根据当前提示确认材料状态后再继续');
        break;
      default:
        break;
    }
  };

  const handleGenerate = () => {
    if (editorWritesBlocked || legacySubmitted || !isValidPositiveId(applicationID) || !ownerLease?.isCurrent() || !isValidPositiveId(resumeID) || !isValidPositiveId(jdVersionID) || !jdSnapshot.trim()) return;

    generateMutation.mutate({
      applicationID,
      generation: ownerLease.generation,
      resumeID,
      jdVersionID,
      overwrite: Boolean(existingKit && existingKit.application_id === applicationID),
    });
  };

  const handleSave = () => {
    if (editorWritesBlocked || legacySubmitted || !existingKit || !isValidPositiveId(applicationID) || !ownerLease?.isCurrent() || existingKit.application_id !== applicationID || !isValidPositiveId(existingKit.id) || !isValidPositiveId(resumeID)) return;

    saveMutation.mutate({
      applicationID,
      generation: ownerLease.generation,
      kitID: existingKit.id,
      resumeID,
      jdSnapshot,
      status: getMaterialKitStatusForSave(existingKit.status, status),
      content: cloneContent(content),
    });
  };

  const handleGenerateProposal = () => {
    if (editorWritesBlocked || proposalDisabled || !isValidPositiveId(applicationID) || !ownerLease?.isCurrent()) return;
    if (proposalAssertionsValidation.error) return;
    proposalMutation.mutate({
      applicationID,
      generation: ownerLease.generation,
      instructions: '',
      userAssertions: proposalAssertionsValidation.values,
    });
  };

  const handleProposalOwnerOperation = (state: MaterialProposalOwnerOperationState) => {
    const currentLease = ownerLeaseRef.current;
    if (!currentLease
      || !applicationID
      || state.applicationID !== applicationID
      || state.generation !== currentLease.generation
      || !currentLease.isCurrent()
      || !proposal
      || proposal.id !== state.proposalID
      || proposal.proposal_sha256 !== state.proposalSha256) return;
    if (state.completed) {
      currentLease.writeProposal(null);
      return;
    }
    currentLease.writeProposal({
      applicationId: state.applicationID,
      proposalId: state.proposalID,
      proposalSha256: state.proposalSha256,
      key: state.key,
      pending: state.pending,
      resultUnknown: state.resultUnknown,
      sourceConflict: state.sourceConflict,
    });
  };

  const handleProposalAccepted = (generation?: number) => {
    if (!applicationID || generation === undefined || !isCurrentOwnerRequest(applicationID, generation)) return;
    queryClient.invalidateQueries({ queryKey: ['resumes'] });
    queryClient.invalidateQueries({ queryKey: ['application-material-kit', applicationID] });
    queryClient.invalidateQueries({ queryKey: ['application-evidence-bundle-preview', applicationID] });
    queryClient.invalidateQueries({ queryKey: ['application-evidence-bundles', applicationID] });
    queryClient.invalidateQueries({ queryKey: ['application-events', applicationID] });
    queryClient.invalidateQueries({ queryKey: ['events'] });
    queryClient.invalidateQueries({ queryKey: ['events', applicationID] });
    queryClient.invalidateQueries({ queryKey: ['application-material-revision-proposals', applicationID] });
    setProposalReviewOpen(false);
    setProposal(null);
    message.success(MATERIAL_FLOW_COPY.drawer.proposalAccepted);
  };

  const openConfirmation = () => {
    if (confirmationWriteBlocked || !canConfirm || confirmationOpen || !ownerLease?.isCurrent()) return;

    const sessionID = crypto.randomUUID();
    const submittedAt = toLocalDateTimeInputValue(new Date());
    confirmationSessionRef.current = sessionID;
    setConfirmationKey(sessionID);
    setConfirmationSubmittedAt(submittedAt);
    setConfirmationError(null);
    setConfirmationOpen(true);
    ownerLease?.writeConfirmation({
      open: true,
      key: sessionID,
      submittedAt,
      error: null,
      previewValid: true,
      pending: false,
      resultUnknown: false,
      sourceConflict: false,
    });
  };

  const closeConfirmation = () => {
    if (confirmMutation.isPending) return;

    const unresolved = confirmationResultUnknown || confirmationSourceConflict || ownerLease?.read().confirmation?.pending === true;
    setConfirmationOpen(false);
    if (unresolved) {
      // Hide the dialog, but retain the session/idempotency identity for a
      // later reopen.  The owner remains blocked until it is resolved.
      ownerLease?.patchConfirmation({ open: true });
      return;
    }
    setConfirmationKey(null);
    setConfirmationSubmittedAt('');
    setConfirmationError(null);
    confirmationSessionRef.current = null;
    ownerLease?.writeConfirmation(null);
  };

  const openEvidenceDetail = async (bundleID: number) => {
    if (!applicationID || !isValidPositiveId(bundleID) || !ownerLease?.isCurrent() || evidenceDetailLoading) return;

    const requestedApplicationID = applicationID;
    const requestedGeneration = ownerLease.generation;
    setEvidenceDetailOpen(true);
    setEvidenceDetail(null);
    setEvidenceDetailError(null);
    setEvidenceDetailLoading(true);
    try {
      const detail = await getEvidenceBundle(requestedApplicationID, bundleID);
      if (isCurrentOwnerRequest(requestedApplicationID, requestedGeneration)) {
        setEvidenceDetail(isValidEvidenceDetailForApplication(detail, requestedApplicationID, bundleID) ? detail : null);
        if (!isValidEvidenceDetailForApplication(detail, requestedApplicationID, bundleID)) {
          setEvidenceDetailError('本次投递记录来源无效');
        }
      }
    } catch (error) {
      if (isCurrentOwnerRequest(requestedApplicationID, requestedGeneration)) {
        setEvidenceDetailError(getErrorMessage(error));
      }
    } finally {
      if (isCurrentOwnerRequest(requestedApplicationID, requestedGeneration)) {
        setEvidenceDetailLoading(false);
      }
    }
  };

  const closeEvidenceDetail = () => {
    if (evidenceDetailLoading) return;

    setEvidenceDetailOpen(false);
    setEvidenceDetail(null);
    setEvidenceDetailError(null);
  };

  const handleConfirm = () => {
    const preview = evidencePreviewQuery.data;
    if (confirmationWriteBlocked
      || !applicationID
      || !ownerLease?.isCurrent()
      || confirmationRefreshing
      || !confirmationPreviewValid
      || !isValidEvidencePreviewForApplication(preview, applicationID)
      || !preview.ready
      || !confirmationKey
      || !confirmationSubmittedAt) return;
    const generation = ownerLease.generation;

    const submittedDate = new Date(confirmationSubmittedAt);
    if (Number.isNaN(submittedDate.getTime())) {
      setConfirmationError('请选择有效的投递时间');
      return;
    }

    ownerLease?.patchConfirmation({ pending: true, open: true });
    confirmMutation.mutate({
      applicationID,
      generation,
      sessionID: confirmationKey,
      input: {
        submitted_at: submittedDate.toISOString(),
        idempotency_key: confirmationKey,
        expected_bundle_sha256: preview.bundle_sha256,
      },
    });
  };

  const updateAdvice = <K extends keyof MaterialKitContent['resume_advice']>(
    key: K,
    value: MaterialKitContent['resume_advice'][K],
  ) => {
    markOwnerDraftDirty();
    setContent((prev) => ({
      ...prev,
      resume_advice: {
        ...prev.resume_advice,
        [key]: value,
      },
    }));
  };

  const updateMessage = (index: number, patch: Partial<MaterialKitMessage>) => {
    markOwnerDraftDirty();
    setContent((prev) => ({
      ...prev,
      messages: prev.messages.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)),
    }));
  };

  const updateChecklist = (id: string, patch: Partial<MaterialKitChecklistItem>) => {
    markOwnerDraftDirty();
    setContent((prev) => ({
      ...prev,
      checklist: prev.checklist.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    }));
  };

  const copyMessageBody = async (body: string) => {
    if (!navigator.clipboard?.writeText) {
      message.error('当前浏览器不支持复制');
      return;
    }

    try {
      await navigator.clipboard.writeText(body);
      message.success('已复制到剪贴板');
    } catch {
      message.error('复制失败，请手动复制');
    }
  };

  const evidencePreviewLoading = evidencePreviewQuery.isFetching && !evidencePreviewQuery.data;
  const evidenceHistoryLoading = evidenceHistoryQuery.isFetching && !evidenceHistoryQuery.data;
  const evidenceHistory: EvidenceBundleSummary[] = evidenceHistoryQuery.data || [];
  const latestEvidenceConfirmation = evidenceHistory.reduce<EvidenceBundleSummary | null>(
    (latest, entry) => (
      latest === null || dayjs(entry.confirmed_at).isAfter(dayjs(latest.confirmed_at)) ? entry : latest
    ),
    null,
  );
  const confirmationPreview = confirmationPreviewValid && !confirmationRefreshing && !evidencePreviewQuery.isError
    && isValidEvidencePreviewForApplication(evidencePreviewQuery.data, applicationID ?? 0)
    ? evidencePreviewQuery.data
    : undefined;

  if (!open) return null;

  return (
    <section className={styles.workspace} aria-label={MATERIAL_FLOW_COPY.drawer.materialKitTitle}>
      <div data-testid="material-kit-surface-state" data-state={materialSurface.state} hidden />
      <div className={styles.workspaceHeader}>
        <Button type="link" icon={<ArrowLeftOutlined />} className={styles.backButton} onClick={onClose}>
          返回投递详情
        </Button>
        <Typography.Title level={3} className={styles.workspaceTitle}>
          {MATERIAL_FLOW_COPY.drawer.materialKitTitle}
        </Typography.Title>
      </div>
      <Spin spinning={kitQuery.isFetching && !kitQuery.data}>
        <div className={styles.layout}>
          <aside className={styles.contextPanel}>
            <div>
              <Typography.Text className={styles.eyebrow}>当前岗位</Typography.Text>
              <Typography.Title level={4} className={styles.company}>
                {application?.company_name || '未选择公司'}
              </Typography.Title>
              <Typography.Paragraph className={styles.position}>
                {application?.position_name || '请选择一个投递记录'}
              </Typography.Paragraph>
              <SourceStateTag state="current" detail="当前投递" />
            </div>

            <Form layout="vertical" className={styles.contextForm}>
              <Form.Item label="简历版本" required>
                <Select
                  id="material-kit-resume-select"
                  placeholder="选择本次实际使用的简历版本"
                  value={resumeID}
                  onChange={(value: number | undefined) => {
                    setResumeID(value);
                    markOwnerDraftDirty();
                  }}
                  options={resumeOptions}
                  loading={resumesQuery.isFetching}
                  disabled={!open || resumesQuery.isFetching || legacySubmitted || editorWritesBlocked}
                  showSearch
                  optionFilterProp="label"
                />
              </Form.Item>

              <Form.Item label="JD 摘要 / 岗位要求" required>
                <Input.TextArea
                  value={jdSnapshot}
                  readOnly
                  aria-readonly="true"
                  onChange={(event) => setJdSnapshot(event.target.value)}
                  placeholder="粘贴岗位 JD，或使用投递备注作为默认内容"
                  rows={8}
                  disabled={!application || legacySubmitted || editorWritesBlocked}
                />
              </Form.Item>

              <Form.Item label={MATERIAL_FLOW_COPY.drawer.candidateFactsLabel}>
                <Input.TextArea
                  value={proposalAssertions}
                  onChange={(event) => {
                    markOwnerDraftDirty();
                    setProposalAssertions(event.target.value);
                  }}
                  placeholder={MATERIAL_FLOW_COPY.drawer.candidateFactsPlaceholder}
                  rows={4}
                  disabled={!application || legacySubmitted || editorWritesBlocked}
                />
                {proposalAssertionsValidation.error ? (
                  <Typography.Text type="danger">{proposalAssertionsValidation.error}</Typography.Text>
                ) : null}
              </Form.Item>

              <Form.Item label="材料状态">
                <Select
                  value={legacySubmitted ? undefined : status}
                  onChange={(nextStatus: EditableMaterialKitStatus) => {
                    setStatus(nextStatus);
                    markOwnerDraftDirty();
                  }}
                  options={EDITABLE_STATUS_OPTIONS}
                disabled={!canSave || legacySubmitted || editorWritesBlocked}
                />
              </Form.Item>
            </Form>

            <div className={styles.progressBlock}>
              <div className={styles.progressHeader}>
                <span>完成度</span>
                <span className="op-tnum">{completion}%</span>
              </div>
              <Progress percent={completion} showInfo={false} className="op-tnum" />
            </div>

            {actionError ? (
              <Alert type="error" showIcon message={actionError} className={styles.alert} />
            ) : null}

            {legacySubmitted ? (
              <Alert
                type="warning"
                showIcon
                message="旧投递标记，缺少证据快照"
                className={styles.legacyWarning}
              />
            ) : null}

            <Space className={styles.actionBar}>
              <Button
                type={fallbackSurfaceAction || materialSurface.primaryAction.id === 'generate' ? 'primary' : 'default'}
                data-material-primary={fallbackSurfaceAction || materialSurface.primaryAction.id === 'generate' ? 'true' : undefined}
                icon={<ReloadOutlined />}
                onClick={fallbackSurfaceAction ? handleSurfaceFallbackAction : handleGenerate}
                loading={generateMutation.isPending}
                disabled={materialSurface.primaryAction.id === 'none' || (!fallbackSurfaceAction && (generateDisabled || editorWritesBlocked))}
              >
                {materialSurface.primaryAction.id === 'generate' || !fallbackSurfaceAction
                  ? '生成材料包'
                  : materialSurface.primaryAction.label}
              </Button>
              <Button
                type={materialSurface.primaryAction.id === 'save' ? 'primary' : 'default'}
                data-material-primary={materialSurface.primaryAction.id === 'save' ? 'true' : undefined}
                icon={<SaveOutlined />}
                onClick={handleSave}
                loading={saveMutation.isPending}
                disabled={!canSave || legacySubmitted || editorWritesBlocked}
              >
                保存
              </Button>
              <Button
                onClick={handleGenerateProposal}
                loading={proposalMutation.isPending}
                disabled={proposalDisabled || editorWritesBlocked || Boolean(proposalAssertionsValidation.error)}
              >
                {MATERIAL_FLOW_COPY.drawer.generateProposal}
              </Button>
              {canConfirm ? (
                <Button
                  type={materialSurface.primaryAction.id === 'record_submission' || materialSurface.primaryAction.id === 'confirm' ? 'primary' : 'default'}
                  data-material-primary={materialSurface.primaryAction.id === 'record_submission' || materialSurface.primaryAction.id === 'confirm' ? 'true' : undefined}
                  onClick={openConfirmation}
                  disabled={confirmationWriteBlocked || confirmationOpen}
                >
                  确认已投递
                </Button>
              ) : null}
              {materialSurface.primaryAction.id === 'view_submission' ? (
                <Button
                  type="primary"
                  data-material-primary="true"
                  onClick={() => document.querySelector('[data-testid="evidence-history"]')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
                >
                  查看本次投递记录
                </Button>
              ) : null}
            </Space>

            <section className={styles.evidenceHistory} data-testid="evidence-history" aria-label={MATERIAL_FLOW_COPY.drawer.evidenceHistoryTitle}>
              <Typography.Text className={styles.evidenceHistoryTitle}>{MATERIAL_FLOW_COPY.drawer.evidenceHistoryTitle}</Typography.Text>
              {evidenceHistoryQuery.isError ? (
                <div className={styles.historyError}>
                  <Typography.Text>本次投递记录加载失败</Typography.Text>
                  <Button size="small" onClick={() => void evidenceHistoryQuery.refetch()}>
                    重新加载历史
                  </Button>
                </div>
              ) : evidenceHistoryLoading ? (
                <Typography.Text className={styles.evidenceEmpty}>正在加载本次投递记录，请稍候</Typography.Text>
              ) : evidenceHistory.length === 0 ? (
                <Typography.Text className={styles.evidenceEmpty}>尚无本次投递记录</Typography.Text>
              ) : (
                <>
                  <div className={styles.evidenceHistorySummary}>
                    <Typography.Text>已确认投递 {evidenceHistory.length} 次</Typography.Text>
                    {latestEvidenceConfirmation ? (
                      <Typography.Text className={styles.evidenceTime}>
                        最近确认（本地）：{formatEvidenceTimestamp(latestEvidenceConfirmation.confirmed_at)}
                      </Typography.Text>
                    ) : null}
                  </div>
                  <div className={styles.evidenceHistoryList}>
                    {evidenceHistory.map((entry) => (
                      <div className={styles.evidenceHistoryItem} key={entry.id}>
                        <Typography.Text>第 {entry.sequence} 次</Typography.Text>
                        <Typography.Text className={styles.evidenceTime}>投递（本地）：{formatEvidenceTimestamp(entry.submitted_at)}</Typography.Text>
                        <Typography.Text className={styles.evidenceTime}>确认（本地）：{formatEvidenceTimestamp(entry.confirmed_at)}</Typography.Text>
                        <Typography.Text className={styles.evidenceTime}>确认方式：{formatConfirmationKind(entry.confirmation_kind)}</Typography.Text>
                        <Button
                          className={styles.evidenceDetailButton}
                          size="small"
                          onClick={() => void openEvidenceDetail(entry.id)}
                          disabled={evidenceDetailLoading}
                        >
                          查看详情
                        </Button>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </section>
          </aside>

          <main className={styles.editorPanel}>
            {!canSave ? (
              <Empty
                className={styles.empty}
                description="选择简历并填写 JD 后，生成材料包即可编辑简历建议、沟通话术和检查清单"
              />
            ) : (
              <div className={styles.sections}>
                <section className={styles.section}>
                  <div className={styles.sectionHeader}>
                    <div>
                      <Typography.Title level={5} className={styles.sectionTitle}>
                        简历优化建议
                      </Typography.Title>
                      <Typography.Text className={styles.sectionHint}>把 AI 建议整理成可执行的修改清单</Typography.Text>
                    </div>
                    <Tag color={displayedStatus === 'submitted' ? 'green' : displayedStatus === 'ready' ? 'blue' : 'default'}>
                      {STATUS_LABELS[displayedStatus]}
                    </Tag>
                  </div>

                  <Form layout="vertical">
                    <Form.Item label="整体摘要">
                      <Input.TextArea
                        value={content.resume_advice.summary}
                        onChange={(event) => updateAdvice('summary', event.target.value)}
                        rows={3}
                        disabled={legacySubmitted || editorWritesBlocked}
                      />
                    </Form.Item>
                    <Form.Item label="匹配亮点">
                      <Input.TextArea
                        value={linesToText(content.resume_advice.highlights)}
                        onChange={(event) => updateAdvice('highlights', textToLines(event.target.value))}
                        rows={4}
                        placeholder="每行一条亮点"
                        disabled={legacySubmitted || editorWritesBlocked}
                      />
                    </Form.Item>
                    <Form.Item label="建议改写的要点">
                      <Input.TextArea
                        value={linesToText(content.resume_advice.rewrite_bullets)}
                        onChange={(event) => updateAdvice('rewrite_bullets', textToLines(event.target.value))}
                        rows={4}
                        placeholder="每行一条改写建议"
                        disabled={legacySubmitted || editorWritesBlocked}
                      />
                    </Form.Item>
                    <Form.Item label="风险缺口">
                      <Input.TextArea
                        value={linesToText(content.resume_advice.gaps)}
                        onChange={(event) => updateAdvice('gaps', textToLines(event.target.value))}
                        rows={3}
                        placeholder="每行一个待补强点"
                        disabled={legacySubmitted || editorWritesBlocked}
                      />
                    </Form.Item>
                    <Form.Item label="备注">
                      <Input.TextArea
                        value={content.resume_advice.notes}
                        onChange={(event) => updateAdvice('notes', event.target.value)}
                        rows={3}
                        disabled={legacySubmitted || editorWritesBlocked}
                      />
                    </Form.Item>
                  </Form>
                </section>

                <section className={styles.section}>
                  <Typography.Title level={5} className={styles.sectionTitle}>
                    沟通话术
                  </Typography.Title>
                  <div className={styles.messageList}>
                    {content.messages.map((item, index) => (
                      <div className={styles.messageItem} key={`${item.type}-${index}`}>
                        <div className={styles.messageHeader}>
                          <Input
                            value={item.title}
                            onChange={(event) => updateMessage(index, { title: event.target.value })}
                            className={styles.messageTitleInput}
                            disabled={legacySubmitted || editorWritesBlocked}
                          />
                          <Button
                            icon={<CopyOutlined />}
                          onClick={() => copyMessageBody(item.body)}
                            disabled={legacySubmitted || editorWritesBlocked || !item.body.trim()}
                          >
                            复制
                          </Button>
                        </div>
                        <Input.TextArea
                          value={item.body}
                          onChange={(event) => updateMessage(index, { body: event.target.value })}
                          rows={5}
                          placeholder="填写可直接发送的正文"
                          disabled={legacySubmitted || editorWritesBlocked}
                        />
                        <Input.TextArea
                          value={item.notes}
                          onChange={(event) => updateMessage(index, { notes: event.target.value })}
                          rows={2}
                          placeholder="内部备注"
                          disabled={legacySubmitted || editorWritesBlocked}
                        />
                      </div>
                    ))}
                  </div>
                </section>

                <section className={styles.section}>
                  <Typography.Title level={5} className={styles.sectionTitle}>
                    投递检查清单
                  </Typography.Title>
                  <div className={styles.checklist}>
                    {content.checklist.map((item) => (
                      <div className={styles.checkRow} key={item.id}>
                        <Checkbox
                          checked={item.done}
                          aria-label={`${item.done ? '取消完成' : '标记完成'}：${item.label}`}
                          onChange={(event) => updateChecklist(item.id, { done: event.target.checked })}
                          disabled={legacySubmitted || editorWritesBlocked}
                        />
                        <Input
                          value={item.label}
                          onChange={(event) => updateChecklist(item.id, { label: event.target.value })}
                          bordered={false}
                          disabled={legacySubmitted || editorWritesBlocked}
                        />
                      </div>
                    ))}
                  </div>
                </section>
              </div>
            )}
          </main>
        </div>
      </Spin>
      <MaterialProposalReviewModal
        applicationID={applicationID || 0}
        proposal={proposal}
        open={proposalReviewOpen}
        writeBlocked={ownerStateBlocksWrites || editorWritesBlocked}
        ownerGeneration={ownerLease?.generation}
        onOwnerOperationStateChange={handleProposalOwnerOperation}
        onClose={() => setProposalReviewOpen(false)}
        onAccepted={handleProposalAccepted}
      />
      <Modal
        open={confirmationOpen}
        title="确认本次投递记录"
        onCancel={closeConfirmation}
        destroyOnClose
        footer={(
          <Space className={styles.confirmationActions}>
            <Button onClick={closeConfirmation} disabled={confirmMutation.isPending}>
              取消
            </Button>
            {!confirmationPreviewValid || evidencePreviewQuery.isError ? (
              <Button
                onClick={() => {
                  if (applicationID && confirmationKey && ownerLease?.isCurrent()) {
                    void refreshEvidencePreview(applicationID, confirmationKey, ownerLease.generation);
                  }
                }}
                loading={confirmationRefreshing}
                disabled={confirmationRefreshing || !applicationID || !confirmationKey}
              >
                重新刷新证据
              </Button>
            ) : null}
            <Button
              type="primary"
              onClick={handleConfirm}
              loading={confirmMutation.isPending}
              disabled={confirmationWriteBlocked || confirmationRefreshing || evidencePreviewQuery.isError || !confirmationPreviewValid || !confirmationPreview?.ready || !confirmationKey || !confirmationSubmittedAt}
            >
              确认投递
            </Button>
          </Space>
        )}
      >
        <ConfirmationPanel
          title="确认本次投递记录"
          description="请核对当前展示的来源后再保存；保存不会替你执行平台操作。"
          sources={confirmationPreview?.ready
            ? [{ state: 'pending', detail: '待确认的本次投递记录预览' }]
            : []}
          className={styles.confirmationBody}
        >
          <Typography.Text className={styles.confirmationKind}>用户确认，非平台回执</Typography.Text>
          <Typography.Paragraph className={styles.confirmationHint}>
            请根据下方只读来源摘要核对本次投递；保存后会保留这份材料快照。
          </Typography.Paragraph>

          {confirmationPreview?.ready ? (
            <div className={styles.sourceSummary}>
              <div className={styles.sourceRow}>
                <Typography.Text>岗位：{confirmationPreview.sources.application.company_name} · {confirmationPreview.sources.application.position_name}</Typography.Text>
              </div>
              <Typography.Text>简历：{confirmationPreview.sources.resume.title}</Typography.Text>
              <Typography.Text>JD：{confirmationPreview.sources.jd.characters} 字符</Typography.Text>
              <Typography.Text>来源已通过当前材料校验。</Typography.Text>
            </div>
          ) : (
            <div className={styles.previewIssues}>
              {confirmationRefreshing ? (
                <Typography.Text>正在刷新材料证据，请稍候</Typography.Text>
              ) : evidencePreviewLoading ? (
                <Typography.Text>正在加载材料证据，请稍候</Typography.Text>
              ) : evidencePreviewQuery.isError ? (
                <Typography.Text>材料证据加载失败，请刷新后再确认</Typography.Text>
              ) : (confirmationPreview?.issues || []).length > 0 ? (
                <ul>
                  {materialEvidencePreviewIssueLabels(confirmationPreview?.issues || []).map((issue) => <li key={issue}>{issue}</li>)}
                </ul>
              ) : (
                <Typography.Text>材料证据尚未准备完成</Typography.Text>
              )}
            </div>
          )}

          <Form layout="vertical">
            <Form.Item label="投递时间">
              <Input
                type="datetime-local"
                value={confirmationSubmittedAt}
                onChange={(event) => setConfirmationSubmittedAt(event.target.value)}
                disabled={confirmationWriteBlocked || confirmationRefreshing || !confirmationPreview?.ready || confirmMutation.isPending}
              />
            </Form.Item>
          </Form>

          {confirmationError ? <Alert type="error" showIcon message={confirmationError} /> : null}
        </ConfirmationPanel>
      </Modal>
      <Modal
        open={evidenceDetailOpen}
        title="本次投递记录详情"
        onCancel={closeEvidenceDetail}
        destroyOnClose
        footer={(
          <Button onClick={closeEvidenceDetail} disabled={evidenceDetailLoading}>
            关闭
          </Button>
        )}
      >
        <div className={styles.evidenceDetailBody}>
          <Typography.Text className={styles.confirmationKind}>只读证据快照</Typography.Text>
          {evidenceDetailLoading ? (
            <Typography.Text className={styles.evidenceEmpty}>正在加载本次投递记录详情，请稍候</Typography.Text>
          ) : evidenceDetailError ? (
            <Alert type="error" showIcon message={evidenceDetailError} />
          ) : evidenceDetail ? (
            <>
              <div className={styles.evidenceDetailSummary}>
                <Typography.Text>第 {evidenceDetail.sequence} 次</Typography.Text>
                <Typography.Text className={styles.evidenceTime}>投递（本地）：{formatEvidenceTimestamp(evidenceDetail.submitted_at)}</Typography.Text>
                <Typography.Text className={styles.evidenceTime}>确认（本地）：{formatEvidenceTimestamp(evidenceDetail.confirmed_at)}</Typography.Text>
                <Typography.Text className={styles.evidenceTime}>确认方式：{formatConfirmationKind(evidenceDetail.confirmation_kind)}</Typography.Text>
              </div>
            </>
          ) : null}
        </div>
      </Modal>
    </section>
  );
}
