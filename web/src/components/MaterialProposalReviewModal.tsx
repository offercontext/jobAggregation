import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Checkbox, Modal, Space, Tag, Typography } from 'antd';
import type { MaterialRevisionProposal } from '@/types/materialRevisionProposal';
import { acceptMaterialRevisionProposal, rejectMaterialRevisionProposal } from '@/services/materialRevisionProposals';
import {
  isMaterialFlowSourceConflict,
  MATERIAL_FLOW_COPY,
  materialEvidenceSourceLabel,
  materialFlowErrorMessage,
} from './materialFlowCopy';
import styles from './MaterialProposalReviewModal.module.css';
import { storySourceLabel } from '@/lib/storySourcePresentation';

function resumeChangeLabel(path: string): string {
  const normalized = path.startsWith('/content_json/') ? path : `/content_json${path}`;
  return storySourceLabel('resume_version', normalized);
}

function evidenceLocationLabel(source: string, path: string): string {
  if (source === 'resume') return resumeChangeLabel(path);
  if (source === 'evidence_bundle' && path.startsWith('/resume/content_json/')) {
    return resumeChangeLabel(path.slice('/resume'.length));
  }
  if (source === 'user_assertion') {
    const match = /^\/user_assertions\/(\d+)\/text$/.exec(path);
    return match ? `我的补充说明 ${Number(match[1]) + 1}` : '我的补充说明';
  }
  return '投递参考内容';
}

export interface MaterialProposalOwnerOperationState {
  applicationID: number;
  proposalID: number;
  proposalSha256: string;
  generation: number;
  key: string;
  pending: boolean;
  resultUnknown: boolean;
  sourceConflict: boolean;
  completed?: boolean;
}

interface Props {
  applicationID: number;
  proposal: MaterialRevisionProposal | null;
  open: boolean;
  /** The canonical material owner may be pending/unknown/source-conflicted. */
  writeBlocked?: boolean;
  /** The owner generation captured when this proposal view was opened. */
  ownerGeneration?: number;
  /** Reports proposal writes to the application-scoped owner store. */
  onOwnerOperationStateChange?: (state: MaterialProposalOwnerOperationState) => void;
  onClose: () => void;
  onAccepted: (generation?: number) => void;
}

function isValidPositiveId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function hasDeletionMarker(value: unknown): boolean {
  try {
    return isRecord(value)
      && (value.deleted === true || value.deleted_at != null || value.deletedAt != null);
  } catch {
    return true;
  }
}

function isValidProposalForApplication(
  value: unknown,
  applicationID: number,
): value is MaterialRevisionProposal {
  try {
    if (!isValidPositiveId(applicationID) || !isRecord(value)) return false;
    const proposal = value as unknown as Record<string, unknown>;
    const source = proposal.source;
    if (!isRecord(source)) return false;
    const sourceApplication = source.application;
    const sourceKit = source.material_kit;
    const sourceResume = source.resume;
    const assertions = source.user_assertions;
    const changes = proposal.changes;
    const acceptedChangeIDs = proposal.accepted_change_ids;
    if (!isRecord(sourceApplication) || !isRecord(sourceKit) || !isRecord(sourceResume)
      || hasDeletionMarker(value)
      || hasDeletionMarker(sourceApplication)
      || hasDeletionMarker(sourceKit)
      || hasDeletionMarker(sourceResume)) return false;
    if (!Array.isArray(assertions) || !assertions.every((assertion) => (
      isRecord(assertion)
      && typeof assertion.id === 'string'
      && assertion.id.length > 0
      && typeof assertion.text === 'string'
    ))) return false;
    if (!Array.isArray(acceptedChangeIDs) || !acceptedChangeIDs.every((id) => typeof id === 'string')) return false;
    if (!Array.isArray(changes) || !changes.every((change) => (
      isRecord(change)
      && typeof change.id === 'string'
      && change.id.length > 0
      && typeof change.path === 'string'
      && typeof change.before === 'string'
      && typeof change.after === 'string'
      && typeof change.rationale === 'string'
      && Array.isArray(change.evidence_refs)
      && change.evidence_refs.every((ref) => (
        isRecord(ref)
        && (ref.source === 'resume' || ref.source === 'evidence_bundle' || ref.source === 'user_assertion')
        && typeof ref.path === 'string'
        && typeof ref.excerpt === 'string'
      ))
    ))) return false;
    return isValidPositiveId(proposal.id)
      && proposal.application_id === applicationID
      && isValidPositiveId(proposal.material_kit_id)
      && typeof proposal.proposal_sha256 === 'string'
      && proposal.proposal_sha256.length > 0
      && proposal.status === 'draft'
      && typeof proposal.summary === 'string'
      && typeof proposal.created_at === 'string'
      && isValidPositiveId(sourceApplication.id)
      && sourceApplication.id === applicationID
      && typeof sourceApplication.company_name === 'string'
      && typeof sourceApplication.position_name === 'string'
      && isValidPositiveId(sourceKit.id)
      && sourceKit.id === proposal.material_kit_id
      && typeof sourceKit.jd_excerpt === 'string'
      && isValidPositiveId(sourceResume.id)
      && sourceResume.id === proposal.source_resume_id
      && typeof sourceResume.title === 'string';
  } catch {
    return false;
  }
}

function readProposalIdentity(value: unknown): { id: number; sha256: string } | null {
  try {
    if (!isRecord(value) || !isValidPositiveId(value.id) || typeof value.proposal_sha256 !== 'string' || !value.proposal_sha256) {
      return null;
    }
    return { id: value.id, sha256: value.proposal_sha256 };
  } catch {
    return null;
  }
}

export default function MaterialProposalReviewModal({
  applicationID,
  proposal,
  open,
  writeBlocked = false,
  ownerGeneration,
  onOwnerOperationStateChange,
  onClose,
  onAccepted,
}: Props) {
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sourceConflict, setSourceConflict] = useState(false);

  useEffect(() => {
    if (!proposal || !isValidProposalForApplication(proposal, applicationID)) {
      setSelected([]);
      setConfirmOpen(false);
      setError(null);
      setSourceConflict(false);
      return;
    }
    setSelected(proposal.changes.map((change) => change.id));
    setConfirmOpen(false);
    setError(null);
    setSourceConflict(false);
  }, [applicationID, proposal]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);

  if (!proposal) return null;

  const proposalValid = isValidProposalForApplication(proposal, applicationID);
  const proposalIdentity = proposalValid ? readProposalIdentity(proposal) : null;
  const operationKey = proposalIdentity ? `${proposalIdentity.id}:${proposalIdentity.sha256}` : '';
  const proposalSource = proposalValid ? proposal.source : null;
  const operationGeneration = ownerGeneration ?? 0;
  const reportOperation = (state: Omit<MaterialProposalOwnerOperationState, 'applicationID' | 'proposalID' | 'proposalSha256' | 'generation' | 'key'>) => {
    if (!proposalValid || !proposalIdentity || operationGeneration <= 0) return;
    onOwnerOperationStateChange?.({
      applicationID,
      proposalID: proposalIdentity.id,
      proposalSha256: proposalIdentity.sha256,
      generation: operationGeneration,
      key: operationKey,
      ...state,
    });
  };

  const toggleChange = (id: string, checked: boolean) => {
    setSelected((current) => checked ? [...current, id] : current.filter((value) => value !== id));
    if (!sourceConflict) setError(null);
  };

  const handleAccept = async () => {
    if (!proposalValid || !proposalIdentity || writeBlocked || busy || sourceConflict) return;
    reportOperation({ pending: true, resultUnknown: false, sourceConflict: false });
    setBusy(true);
    setError(null);
    try {
      await acceptMaterialRevisionProposal(applicationID, proposalIdentity.id, {
        expected_proposal_sha256: proposalIdentity.sha256,
        selected_change_ids: selected,
      });
      reportOperation({ pending: false, resultUnknown: false, sourceConflict: false, completed: true });
      setConfirmOpen(false);
      onAccepted(operationGeneration);
    } catch (reason) {
      const conflict = isMaterialFlowSourceConflict(reason);
      if (conflict) setSourceConflict(true);
      reportOperation({ pending: false, resultUnknown: !conflict, sourceConflict: conflict });
      setError(materialFlowErrorMessage(reason, 'proposal'));
    } finally {
      setBusy(false);
    }
  };

  const handleReject = async () => {
    if (!proposalValid || !proposalIdentity || writeBlocked || busy || sourceConflict) return;
    reportOperation({ pending: true, resultUnknown: false, sourceConflict: false });
    setBusy(true);
    setError(null);
    try {
      await rejectMaterialRevisionProposal(applicationID, proposalIdentity.id);
      reportOperation({ pending: false, resultUnknown: false, sourceConflict: false, completed: true });
      onAccepted(operationGeneration);
      onClose();
    } catch (reason) {
      const conflict = isMaterialFlowSourceConflict(reason);
      if (conflict) setSourceConflict(true);
      reportOperation({ pending: false, resultUnknown: !conflict, sourceConflict: conflict });
      setError(materialFlowErrorMessage(reason, 'proposal'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Modal
        open={open}
        title={MATERIAL_FLOW_COPY.proposal.title}
        onCancel={busy ? undefined : onClose}
        destroyOnClose
        footer={(
          <Space>
            <Button onClick={handleReject} disabled={busy || writeBlocked || !proposalValid || sourceConflict}>{MATERIAL_FLOW_COPY.proposal.reject}</Button>
            <Button
              type="primary"
              onClick={() => setConfirmOpen(true)}
              disabled={busy || writeBlocked || !proposalValid || sourceConflict || selected.length === 0}
            >
              {MATERIAL_FLOW_COPY.proposal.accept}
            </Button>
          </Space>
        )}
      >
        <div className={styles.body}>
          <Typography.Text className={styles.warning}>
            {MATERIAL_FLOW_COPY.proposal.warning}
          </Typography.Text>
          {proposalSource ? (
            <div className={styles.source}>
              <Typography.Text strong>
                {proposalSource.application.company_name} · {proposalSource.application.position_name}
              </Typography.Text>
              <Typography.Text>{MATERIAL_FLOW_COPY.proposal.sourceResume}：{proposalSource.resume.title}</Typography.Text>
              <Typography.Text>{MATERIAL_FLOW_COPY.proposal.jdDirection}：{proposalSource.material_kit.jd_excerpt}</Typography.Text>
              <Typography.Text>{MATERIAL_FLOW_COPY.proposal.generatedAt}：{proposal.created_at}</Typography.Text>
            </div>
          ) : null}
          {!proposalValid ? (
            <Alert type="error" showIcon message="提案来源无效，已停止写入" />
          ) : proposal.changes.length === 0 ? (
            <Alert type="info" showIcon message={MATERIAL_FLOW_COPY.proposal.empty} />
          ) : (
            <>
              <Typography.Paragraph>{proposal.summary}</Typography.Paragraph>
              {proposal.changes.map((change) => (
                <div className={styles.change} key={change.id}>
                  <div className={styles.changeHeader}>
                    <Checkbox
                      checked={selectedSet.has(change.id)}
                      onChange={(event) => toggleChange(change.id, event.target.checked)}
                      aria-label={MATERIAL_FLOW_COPY.proposal.selectChange}
                    />
                    <div className={styles.changeText}>
                      <Typography.Text strong>{resumeChangeLabel(change.path)}</Typography.Text>
                      <Typography.Text className={styles.before}>{MATERIAL_FLOW_COPY.proposal.before}：{change.before}</Typography.Text>
                      <Typography.Text className={styles.after}>{MATERIAL_FLOW_COPY.proposal.after}：{change.after}</Typography.Text>
                      <Typography.Text>{MATERIAL_FLOW_COPY.proposal.why}：{change.rationale}</Typography.Text>
                    </div>
                  </div>
                  <div className={styles.evidenceList}>
                    {change.evidence_refs.map((ref) => (
                      <div className={styles.evidence} key={`${change.id}-${ref.source}-${ref.path}`}>
                        <Tag>{materialEvidenceSourceLabel(ref.source)}</Tag>
                        <div>{evidenceLocationLabel(ref.source, ref.path)}：{ref.excerpt}</div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </>
          )}
          {proposalSource?.user_assertions.map((assertion) => (
            <Typography.Text key={assertion.id} type="secondary">
              {MATERIAL_FLOW_COPY.proposal.userAssertion}：{assertion.text}
            </Typography.Text>
          ))}
          {error ? <Alert className={styles.error} type="error" showIcon message={error} /> : null}
        </div>
      </Modal>
      <Modal
        open={confirmOpen}
        title={MATERIAL_FLOW_COPY.proposal.confirmTitle}
        onCancel={() => setConfirmOpen(false)}
        okText={MATERIAL_FLOW_COPY.proposal.createDerivedResume}
        cancelText={MATERIAL_FLOW_COPY.proposal.backToReview}
        onOk={() => void handleAccept()}
        confirmLoading={busy}
        okButtonProps={{ disabled: writeBlocked || busy || !proposalValid || sourceConflict || selected.length === 0 }}
      >
        <Typography.Paragraph>
          {MATERIAL_FLOW_COPY.proposal.confirmBody}
        </Typography.Paragraph>
      </Modal>
    </>
  );
}
