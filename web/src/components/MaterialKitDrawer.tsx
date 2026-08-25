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
import MaterialProposalReviewModal from './MaterialProposalReviewModal';
import { ConfirmationPanel } from './ui/ConfirmationPanel';
import { SourceStateTag } from './ui/SourceStateTag';
import styles from './MaterialKitDrawer.module.css';
import { getMaterialKitStatusForSave } from './materialKitStatus';
import {
  isMaterialFlowSourceConflict,
  MATERIAL_FLOW_COPY,
  materialConfirmationKindLabel,
  materialEvidencePreviewIssueLabels,
  materialFlowErrorMessage,
  type MaterialFlowErrorContext,
} from './materialFlowCopy';

interface Props {
  application: Application | null;
  open: boolean;
  onClose: () => void;
  initialResumeID?: number;
  initialJdSnapshot?: string;
  initialJdVersionID?: number;
}

interface GenerateVariables {
  applicationID: number;
  resumeID: number;
  jdVersionID: number;
  overwrite: boolean;
}

interface SaveVariables {
  applicationID: number;
  kitID: number;
  resumeID: number | undefined;
  jdSnapshot: string;
  status: EditableMaterialKitStatus | undefined;
  content: MaterialKitContent;
}

interface ConfirmVariables {
  applicationID: number;
  sessionID: string;
  input: ConfirmEvidenceBundleInput;
}

interface ProposalVariables {
  applicationID: number;
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

export default function MaterialKitDrawer({ application, open, onClose, initialResumeID, initialJdSnapshot, initialJdVersionID }: Props) {
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const applicationID = application?.id;
  const activeApplicationIDRef = useRef<number | undefined>(applicationID);
  const confirmationSessionRef = useRef<string | null>(null);
  const blockedPreviewUpdatedAtRef = useRef<number | null>(null);
  activeApplicationIDRef.current = applicationID;

  const [existingKit, setExistingKit] = useState<MaterialKitViewModel | null>(null);
  const [resumeID, setResumeID] = useState<number | undefined>();
  const [jdSnapshot, setJdSnapshotState] = useState('');
  const [jdVersionID, setJdVersionID] = useState<number | undefined>(initialJdVersionID);
  const setJdSnapshot = (value: string) => {
    if (!jdVersionID) setJdSnapshotState(value);
  };
  const [status, setStatus] = useState<EditableMaterialKitStatus>('draft');
  const [content, setContent] = useState<MaterialKitContent>(() => createDefaultContent());
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [confirmationKey, setConfirmationKey] = useState<string | null>(null);
  const [confirmationSubmittedAt, setConfirmationSubmittedAt] = useState('');
  const [confirmationError, setConfirmationError] = useState<string | null>(null);
  const [confirmationRefreshing, setConfirmationRefreshing] = useState(false);
  const [confirmationPreviewValid, setConfirmationPreviewValid] = useState(true);
  const [evidenceDetailOpen, setEvidenceDetailOpen] = useState(false);
  const [evidenceDetail, setEvidenceDetail] = useState<EvidenceBundleDetail | null>(null);
  const [evidenceDetailError, setEvidenceDetailError] = useState<string | null>(null);
  const [evidenceDetailLoading, setEvidenceDetailLoading] = useState(false);
  const [proposalReviewOpen, setProposalReviewOpen] = useState(false);
  const [proposal, setProposal] = useState<MaterialRevisionProposal | null>(null);
  const [proposalAssertions, setProposalAssertions] = useState('');
  const proposalAssertionsValidation = useMemo(
    () => validateProposalAssertions(proposalAssertions),
    [proposalAssertions],
  );

  const isCurrentConfirmationSession = (requestedApplicationID: number, sessionID: string) =>
    activeApplicationIDRef.current === requestedApplicationID && confirmationSessionRef.current === sessionID;

  const resetEditor = (nextApplication: Application | null) => {
    setExistingKit(null);
    setResumeID(initialResumeID);
    setJdSnapshotState((initialJdSnapshot ?? nextApplication?.notes) || '');
    setJdVersionID(initialJdVersionID);
    setStatus('draft');
    setContent(createDefaultContent());
    setActionError(null);
    setConfirmationOpen(false);
    setConfirmationKey(null);
    setConfirmationSubmittedAt('');
    setConfirmationError(null);
    setConfirmationRefreshing(false);
    setConfirmationPreviewValid(true);
    setEvidenceDetailOpen(false);
    setEvidenceDetail(null);
    setEvidenceDetailError(null);
    setEvidenceDetailLoading(false);
    setProposalReviewOpen(false);
    setProposal(null);
    setProposalAssertions('');
    confirmationSessionRef.current = null;
    blockedPreviewUpdatedAtRef.current = null;
  };

  const applyKitToEditor = (kit: MaterialKitViewModel) => {
    setExistingKit(kit);
    setResumeID(kit.resume_id);
    setJdSnapshotState(kit.jd_snapshot);
    setJdVersionID(kit.jd_version_id);
    setStatus(kit.status === 'submitted' ? 'draft' : kit.status);
    setContent(cloneContent(kit.content));
    setActionError(null);
  };

  const kitQuery = useQuery({
    queryKey: ['application-material-kit', applicationID],
    queryFn: () => getApplicationMaterialKit(applicationID!),
    enabled: open && Boolean(applicationID),
  });

  const resumesQuery = useQuery({
    queryKey: ['resumes'],
    queryFn: () => listResumes(),
    enabled: open,
  });

  const evidencePreviewQuery = useQuery<EvidenceBundlePreview>({
    queryKey: ['application-evidence-bundle-preview', applicationID],
    queryFn: () => getEvidenceBundlePreview(applicationID!),
    enabled: open && Boolean(applicationID),
  });

  const evidenceHistoryQuery = useQuery({
    queryKey: ['application-evidence-bundles', applicationID],
    queryFn: () => listEvidenceBundles(applicationID!),
    enabled: open && Boolean(applicationID),
  });

  useEffect(() => {
    resetEditor(open ? application : null);
  }, [applicationID, application?.notes, initialJdSnapshot, initialJdVersionID, initialResumeID, open]);

  useEffect(() => {
    if (evidencePreviewQuery.isError) {
      setConfirmationPreviewValid(false);
      return;
    }

    if (!evidencePreviewQuery.isSuccess || !evidencePreviewQuery.data.ready || confirmationRefreshing) return;

    if (
      blockedPreviewUpdatedAtRef.current !== null
      && evidencePreviewQuery.dataUpdatedAt <= blockedPreviewUpdatedAtRef.current
    ) {
      return;
    }

    if (
      confirmationOpen
      && (!applicationID || !confirmationKey || !isCurrentConfirmationSession(applicationID, confirmationKey))
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
  ]);

  useEffect(() => {
    if (!open || !applicationID || !kitQuery.isSuccess) return;

    const kit = kitQuery.data;
    if (!kit) {
      resetEditor(application);
      return;
    }

    if (kit.application_id !== applicationID) return;
    applyKitToEditor(kit);
  }, [application, applicationID, kitQuery.data, kitQuery.isSuccess, open]);

  useEffect(() => {
    if (!kitQuery.isError) return;

    setExistingKit(null);
    setResumeID(undefined);
    setJdSnapshotState(application?.notes || '');
    setJdVersionID(undefined);
    setStatus('draft');
    setContent(createDefaultContent());
    setActionError(getErrorMessage(kitQuery.error));
  }, [application?.notes, kitQuery.error, kitQuery.isError]);

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
      queryClient.setQueryData(['application-material-kit', kit.application_id], kit);

      if (kit.application_id !== applicationID || variables.applicationID !== applicationID) return;

      applyKitToEditor(kit);
      message.success('材料包已生成');
    },
    onError: (error, variables) => {
      if (variables.applicationID === applicationID) {
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
      queryClient.setQueryData(['application-material-kit', kit.application_id], kit);

      if (kit.application_id !== applicationID || variables.applicationID !== applicationID) return;

      applyKitToEditor(kit);
      message.success('材料包已保存');
    },
    onError: (error, variables) => {
      if (variables.applicationID === applicationID) {
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
      if (variables.applicationID !== applicationID) return;
      setProposal(nextProposal);
      setProposalReviewOpen(true);
    },
    onError: (error: unknown, variables: ProposalVariables) => {
      if (variables.applicationID === applicationID) setActionError(getErrorMessage(error, 'proposal'));
    },
  });

  const refreshEvidencePreview = async (requestedApplicationID: number, sessionID: string) => {
    if (!isCurrentConfirmationSession(requestedApplicationID, sessionID)) return;

    setConfirmationRefreshing(true);
    setConfirmationPreviewValid(false);
    try {
      const result = await evidencePreviewQuery.refetch();
      if (!isCurrentConfirmationSession(requestedApplicationID, sessionID)) return;

      if (result.isSuccess && result.data.ready) {
        setConfirmationPreviewValid(true);
        blockedPreviewUpdatedAtRef.current = null;
        return;
      }

      setConfirmationError('材料证据刷新失败，请重试刷新后再确认');
    } catch {
      if (isCurrentConfirmationSession(requestedApplicationID, sessionID)) {
        setConfirmationError('材料证据刷新失败，请重试刷新后再确认');
      }
    } finally {
      if (isCurrentConfirmationSession(requestedApplicationID, sessionID)) {
        setConfirmationRefreshing(false);
      }
    }
  };

  const confirmMutation = useMutation({
    mutationFn: ({ applicationID: requestedApplicationID, input }: ConfirmVariables) =>
      confirmEvidenceBundle(requestedApplicationID, input),
    onSuccess: (_bundle, variables) => {
      queryClient.invalidateQueries({ queryKey: ['application-evidence-bundle-preview', variables.applicationID] });
      queryClient.invalidateQueries({ queryKey: ['application-evidence-bundles', variables.applicationID] });
      queryClient.invalidateQueries({ queryKey: ['events'] });
      queryClient.invalidateQueries({ queryKey: ['events', variables.applicationID] });
      queryClient.invalidateQueries({ queryKey: ['applications'] });
      if (!isCurrentConfirmationSession(variables.applicationID, variables.sessionID)) return;

      setConfirmationOpen(false);
      setConfirmationKey(null);
      setConfirmationError(null);
      setConfirmationRefreshing(false);
      setConfirmationPreviewValid(true);
      confirmationSessionRef.current = null;
      blockedPreviewUpdatedAtRef.current = null;
      message.success('本次投递记录已保存');
    },
    onError: (error: unknown, variables) => {
      if (!isCurrentConfirmationSession(variables.applicationID, variables.sessionID)) return;

      if (isMaterialFlowSourceConflict(error)) {
        setConfirmationError(getErrorMessage(error, 'confirmation'));
        setConfirmationPreviewValid(false);
        blockedPreviewUpdatedAtRef.current = evidencePreviewQuery.dataUpdatedAt;
        void refreshEvidencePreview(variables.applicationID, variables.sessionID);
        return;
      }

      setConfirmationError(getErrorMessage(error, 'confirmation'));
    },
  });

  const resumeOptions = (resumesQuery.data || []).map((resume: Resume) => ({
    label: resume.name,
    value: resume.id,
  }));

  const canSave = Boolean(existingKit && applicationID && existingKit.application_id === applicationID);
  const legacySubmitted = existingKit?.status === 'submitted';
  const canConfirm = Boolean(canSave);
  const displayedStatus: MaterialKitStatus = legacySubmitted ? 'submitted' : status;
  const generateDisabled = !applicationID || !resumeID || !jdVersionID || !jdSnapshot.trim();
  const proposalDisabled = !applicationID || !existingKit || existingKit.application_id !== applicationID || !resumeID || !jdVersionID || !jdSnapshot.trim();
  const busy = kitQuery.isFetching || generateMutation.isPending || saveMutation.isPending || proposalMutation.isPending || confirmMutation.isPending || confirmationRefreshing;

  const handleGenerate = () => {
    if (!applicationID || !resumeID || !jdVersionID || !jdSnapshot.trim()) return;

    generateMutation.mutate({
      applicationID,
      resumeID,
      jdVersionID,
      overwrite: Boolean(existingKit && existingKit.application_id === applicationID),
    });
  };

  const handleSave = () => {
    if (!existingKit || !applicationID || existingKit.application_id !== applicationID) return;

    saveMutation.mutate({
      applicationID,
      kitID: existingKit.id,
      resumeID,
      jdSnapshot,
      status: getMaterialKitStatusForSave(existingKit.status, status),
      content: cloneContent(content),
    });
  };

  const handleGenerateProposal = () => {
    if (proposalDisabled || !applicationID) return;
    if (proposalAssertionsValidation.error) return;
    proposalMutation.mutate({
      applicationID,
      instructions: '',
      userAssertions: proposalAssertionsValidation.values,
    });
  };

  const handleProposalAccepted = () => {
    if (!applicationID) return;
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
    if (!canConfirm || confirmationOpen) return;

    const sessionID = crypto.randomUUID();
    confirmationSessionRef.current = sessionID;
    setConfirmationKey(sessionID);
    setConfirmationSubmittedAt(toLocalDateTimeInputValue(new Date()));
    setConfirmationError(null);
    setConfirmationOpen(true);
  };

  const closeConfirmation = () => {
    if (confirmMutation.isPending) return;

    setConfirmationOpen(false);
    setConfirmationKey(null);
    setConfirmationSubmittedAt('');
    setConfirmationError(null);
    confirmationSessionRef.current = null;
  };

  const openEvidenceDetail = async (bundleID: number) => {
    if (!applicationID || evidenceDetailLoading) return;

    const requestedApplicationID = applicationID;
    setEvidenceDetailOpen(true);
    setEvidenceDetail(null);
    setEvidenceDetailError(null);
    setEvidenceDetailLoading(true);
    try {
      const detail = await getEvidenceBundle(requestedApplicationID, bundleID);
      if (activeApplicationIDRef.current === requestedApplicationID) {
        setEvidenceDetail(detail);
      }
    } catch (error) {
      if (activeApplicationIDRef.current === requestedApplicationID) {
        setEvidenceDetailError(getErrorMessage(error));
      }
    } finally {
      if (activeApplicationIDRef.current === requestedApplicationID) {
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
    if (!applicationID || confirmationRefreshing || !confirmationPreviewValid || !preview?.ready || !confirmationKey || !confirmationSubmittedAt) return;

    const submittedDate = new Date(confirmationSubmittedAt);
    if (Number.isNaN(submittedDate.getTime())) {
      setConfirmationError('请选择有效的投递时间');
      return;
    }

    confirmMutation.mutate({
      applicationID,
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
    setContent((prev) => ({
      ...prev,
      resume_advice: {
        ...prev.resume_advice,
        [key]: value,
      },
    }));
  };

  const updateMessage = (index: number, patch: Partial<MaterialKitMessage>) => {
    setContent((prev) => ({
      ...prev,
      messages: prev.messages.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)),
    }));
  };

  const updateChecklist = (id: string, patch: Partial<MaterialKitChecklistItem>) => {
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
    ? evidencePreviewQuery.data
    : undefined;

  if (!open) return null;

  return (
    <section className={styles.workspace} aria-label={MATERIAL_FLOW_COPY.drawer.materialKitTitle}>
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
                  placeholder="选择本次实际使用的简历版本"
                  value={resumeID}
                  onChange={setResumeID}
                  options={resumeOptions}
                  loading={resumesQuery.isFetching}
                  disabled={!open || resumesQuery.isFetching}
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
                  disabled={!application}
                />
              </Form.Item>

              <Form.Item label={MATERIAL_FLOW_COPY.drawer.candidateFactsLabel}>
                <Input.TextArea
                  value={proposalAssertions}
                  onChange={(event) => setProposalAssertions(event.target.value)}
                  placeholder={MATERIAL_FLOW_COPY.drawer.candidateFactsPlaceholder}
                  rows={4}
                  disabled={!application}
                />
                {proposalAssertionsValidation.error ? (
                  <Typography.Text type="danger">{proposalAssertionsValidation.error}</Typography.Text>
                ) : null}
              </Form.Item>

              <Form.Item label="材料状态">
                <Select
                  value={legacySubmitted ? undefined : status}
                  onChange={(nextStatus: EditableMaterialKitStatus) => setStatus(nextStatus)}
                  options={EDITABLE_STATUS_OPTIONS}
                  disabled={!canSave || legacySubmitted}
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
                type="primary"
                icon={<ReloadOutlined />}
                onClick={handleGenerate}
                loading={generateMutation.isPending}
                disabled={generateDisabled || busy}
              >
                生成材料包
              </Button>
              <Button
                icon={<SaveOutlined />}
                onClick={handleSave}
                loading={saveMutation.isPending}
                disabled={!canSave || busy}
              >
                保存
              </Button>
              <Button
                onClick={handleGenerateProposal}
                loading={proposalMutation.isPending}
                disabled={proposalDisabled || busy || Boolean(proposalAssertionsValidation.error)}
              >
                {MATERIAL_FLOW_COPY.drawer.generateProposal}
              </Button>
              {canConfirm ? (
                <Button type="primary" onClick={openConfirmation} disabled={busy}>
                  确认已投递
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
                        <details>
                          <summary>高级信息</summary>
                          <Typography.Text className={styles.evidenceHash}>{entry.bundle_sha256}</Typography.Text>
                        </details>
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
                      />
                    </Form.Item>
                    <Form.Item label="匹配亮点">
                      <Input.TextArea
                        value={linesToText(content.resume_advice.highlights)}
                        onChange={(event) => updateAdvice('highlights', textToLines(event.target.value))}
                        rows={4}
                        placeholder="每行一条亮点"
                      />
                    </Form.Item>
                    <Form.Item label="建议改写的要点">
                      <Input.TextArea
                        value={linesToText(content.resume_advice.rewrite_bullets)}
                        onChange={(event) => updateAdvice('rewrite_bullets', textToLines(event.target.value))}
                        rows={4}
                        placeholder="每行一条改写建议"
                      />
                    </Form.Item>
                    <Form.Item label="风险缺口">
                      <Input.TextArea
                        value={linesToText(content.resume_advice.gaps)}
                        onChange={(event) => updateAdvice('gaps', textToLines(event.target.value))}
                        rows={3}
                        placeholder="每行一个待补强点"
                      />
                    </Form.Item>
                    <Form.Item label="备注">
                      <Input.TextArea
                        value={content.resume_advice.notes}
                        onChange={(event) => updateAdvice('notes', event.target.value)}
                        rows={3}
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
                          />
                          <Button
                            icon={<CopyOutlined />}
                            onClick={() => copyMessageBody(item.body)}
                            disabled={!item.body.trim()}
                          >
                            复制
                          </Button>
                        </div>
                        <Input.TextArea
                          value={item.body}
                          onChange={(event) => updateMessage(index, { body: event.target.value })}
                          rows={5}
                          placeholder="填写可直接发送的正文"
                        />
                        <Input.TextArea
                          value={item.notes}
                          onChange={(event) => updateMessage(index, { notes: event.target.value })}
                          rows={2}
                          placeholder="内部备注"
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
                        />
                        <Input
                          value={item.label}
                          onChange={(event) => updateChecklist(item.id, { label: event.target.value })}
                          bordered={false}
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
                  if (applicationID && confirmationKey) {
                    void refreshEvidencePreview(applicationID, confirmationKey);
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
              disabled={confirmationRefreshing || evidencePreviewQuery.isError || !confirmationPreviewValid || !confirmationPreview?.ready || !confirmationKey || !confirmationSubmittedAt}
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
              <details>
                <summary>高级信息</summary>
                <div className={styles.sourceRow}>
                  <Typography.Text>简历版本摘要：{confirmationPreview.sources.resume.sha256}</Typography.Text>
                </div>
                <div className={styles.sourceRow}>
                  <Typography.Text>JD 摘要：{confirmationPreview.sources.jd.sha256}</Typography.Text>
                </div>
                <div className={styles.sourceRow}>
                  <Typography.Text>投递准备编号：#{confirmationPreview.sources.material_kit.id}</Typography.Text>
                  <Typography.Text className={styles.evidenceHash}>{confirmationPreview.sources.material_kit.sha256}</Typography.Text>
                </div>
                <div className={styles.bundleHash}>
                  <Typography.Text>本次记录摘要</Typography.Text>
                  <Typography.Text className={styles.evidenceHash}>{confirmationPreview.bundle_sha256}</Typography.Text>
                </div>
              </details>
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
                disabled={confirmationRefreshing || !confirmationPreview?.ready || confirmMutation.isPending}
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
              <details>
                <summary>高级信息</summary>
                <Typography.Text className={styles.evidenceHash}>{evidenceDetail.bundle_sha256}</Typography.Text>
                <pre className={styles.evidenceSnapshot}>{JSON.stringify(evidenceDetail.snapshot, null, 2)}</pre>
              </details>
            </>
          ) : null}
        </div>
      </Modal>
    </section>
  );
}
