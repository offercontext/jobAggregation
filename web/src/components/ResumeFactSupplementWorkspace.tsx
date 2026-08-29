import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Checkbox, Input, Tag } from 'antd';
import { CheckCircleFilled, SafetyCertificateOutlined } from '@ant-design/icons';
import { copyResume, updateResume } from '@/services/resumes';
import type { Resume } from '@/types/resume';
import type { ResumeAuditFinding } from '@/lib/resumeEvidenceAudit';
import { formatResumeLineage } from '@/features/materialSurfaces/materialLabels';
import { resolveResumeLineage } from '@/features/materialSurfaces/resumeLineage';
import { applyResumeFactSupplement, validateSupplementText } from '@/lib/resumeFactSupplement';
import styles from './ResumeLibraryView.module.css';

export interface ResumeFactSupplementWorkspaceProps {
  open: boolean;
  source: Resume;
  resumes?: readonly Resume[];
  finding: ResumeAuditFinding;
  onClose: () => void;
  onCompleted: (resume: Resume) => void;
  onCopyCreated?: (resume: Resume) => void;
  onContinueInCopy?: (resume: Resume) => void;
  onExitToLibrary?: () => void;
  onCopyResultUnknown?: () => void;
}

type OperationState = 'idle' | 'saving' | 'source_changed' | 'unknown';

export default function ResumeFactSupplementWorkspace({
  open,
  source,
  resumes,
  finding,
  onClose,
  onCompleted,
  onCopyCreated,
  onContinueInCopy,
  onExitToLibrary,
  onCopyResultUnknown,
}: ResumeFactSupplementWorkspaceProps) {
  const excerpt = finding.source?.excerpt ?? '';
  const expectedText = finding.source?.fullText ?? excerpt;
  const [finalText, setFinalText] = useState(expectedText);
  const [confirmed, setConfirmed] = useState(false);
  const [operationState, setOperationState] = useState<OperationState>('idle');
  const [createdCopy, setCreatedCopy] = useState<Resume | null>(null);
  const [statusText, setStatusText] = useState('');
  const dialogRef = useRef<HTMLElement | null>(null);
  const versionTitleRef = useRef('');
  const onCloseRef = useRef(onClose);
  const operationStateRef = useRef<OperationState>('idle');
  const lineageResumes = useMemo(() => {
    if (resumes?.some((candidate) => candidate.id === source.id)) return resumes;
    return [source, ...(resumes ?? [])];
  }, [resumes, source]);
  const lineage = useMemo(
    () => resolveResumeLineage(lineageResumes, source.id),
    [lineageResumes, source.id],
  );
  const lineageLabel = formatResumeLineage(lineage);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    setFinalText(expectedText);
    setConfirmed(false);
    operationStateRef.current = 'idle';
    setOperationState('idle');
    setCreatedCopy(null);
    setStatusText('');
    versionTitleRef.current = '';
  }, [expectedText, finding.id, open, source.id]);

  useEffect(() => {
    if (!open) return undefined;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && operationStateRef.current === 'idle') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const dialog = dialogRef.current;
      const focusable = getFocusableElements(dialog);
      if (!dialog || focusable.length === 0) {
        event.preventDefault();
        dialog?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      opener?.focus();
    };
  }, [open]);

  const normalizedText = useMemo(() => {
    try {
      return validateSupplementText(finalText);
    } catch {
      return '';
    }
  }, [finalText]);
  const frozen = operationState === 'unknown' || operationState === 'source_changed';
  const lineageKnown = lineage.kind !== 'relationship_unknown';
  const canSubmit = confirmed
    && normalizedText.length > 0
    && normalizedText !== expectedText.trim()
    && operationState !== 'saving'
    && lineageKnown
    && !frozen;

  if (!open) return null;

  const handleSubmit = async () => {
    try {
      applyResumeFactSupplement(
        source.content_json,
        finding.source?.path ?? '',
        expectedText,
        finalText,
      );
    } catch (error) {
      setStatusText(error instanceof Error ? error.message : '无法应用这条事实补充');
      return;
    }

    operationStateRef.current = 'saving';
    setOperationState('saving');
    setStatusText('正在创建独立版本，原简历不会被修改…');
    let copy = createdCopy;
    if (!copy) {
      const title = versionTitleRef.current || buildVersionTitle(source);
      versionTitleRef.current = title;
      try {
        copy = await copyResume(source.id, { title });
        setCreatedCopy(copy);
        onCopyCreated?.(copy);
      } catch {
        operationStateRef.current = 'unknown';
        setOperationState('unknown');
        setStatusText('创建结果待确认。为避免重复版本，已冻结操作，请返回简历库核对。');
        onCopyResultUnknown?.();
        return;
      }
    }

    let patchedContent: Resume['content_json'];
    try {
      patchedContent = applyResumeFactSupplement(
        copy.content_json,
        finding.source?.path ?? '',
        expectedText,
        finalText,
      );
    } catch {
      operationStateRef.current = 'source_changed';
      setOperationState('source_changed');
      setStatusText('来源已变化。新版本已创建，但没有写入旧表述，请返回简历库重新体检。');
      return;
    }

    setStatusText('新版本已创建，正在保存经你确认的事实…');
    try {
      const updated = await updateResume(copy.id, { content_json: patchedContent });
      setStatusText('事实补充版本已保存，即将打开版本差异。');
      onCompleted(updated);
    } catch {
      operationStateRef.current = 'unknown';
      setOperationState('unknown');
      setStatusText('新版本已创建，但保存结果待确认。为避免覆盖已写入内容，请返回简历库核对。');
      onCopyResultUnknown?.();
    }
  };

  return (
    <div className={styles.factWorkspaceBackdrop} role="presentation">
      <section
        ref={dialogRef}
        className={styles.factWorkspace}
        role="dialog"
        aria-modal="true"
        aria-label={`补充真实事实：${finding.title}`}
        tabIndex={-1}
      >
        <header className={styles.factWorkspaceHeader}>
          <div>
            <div className={styles.factWorkspaceEyebrow}>事实补充工作台</div>
            <h2>把可核验事实写进新版本</h2>
            <p>原简历保持不变；完成后会自动打开修改前后对比。</p>
          </div>
          <button
            type="button"
            className={styles.compareClose}
            aria-label="关闭事实补充工作台"
            disabled={operationState !== 'idle'}
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <div className={styles.factWorkspaceBody}>
          <section className={styles.factSourceColumn} aria-label="当前来源">
            <div className={styles.factSectionHeading}>
              <span>01</span>
              <div>
                <strong>核对当前来源</strong>
                <small>这段原文来自已保存简历</small>
              </div>
            </div>
            <div className={styles.factSourceCard}>
              <Tag color={lineage.kind === 'base' ? 'blue' : lineage.kind === 'relationship_unknown' ? 'warning' : 'default'}>{lineageLabel}</Tag>
              <span>来自已保存简历的当前内容</span>
              <blockquote>{excerpt || '（空白经历要点）'}</blockquote>
            </div>
            <div className={styles.factGuardrail}>
              <SafetyCertificateOutlined />
              <div>
                <strong>系统不会替你估算数字</strong>
                <p>没有可确认的范围或结果时，保留真实的定性描述即可。</p>
              </div>
            </div>
          </section>

          <section className={styles.factEditorColumn} aria-label="确认后的事实">
            <div className={styles.factSectionHeading}>
              <span>02</span>
              <div>
                <strong>写下你能确认的表述</strong>
                <small>先核对事实，再决定如何写进简历</small>
              </div>
            </div>
            <div className={styles.factPrompts} aria-label="事实核对提示">
              <span>实际采取了什么行动？</span>
              <span>范围或数字有来源吗？</span>
              <span>结果如何被验证？</span>
            </div>
            <label className={styles.factTextareaLabel} htmlFor="resume-fact-final-text">
              经确认的最终简历表述
            </label>
            <Input.TextArea
              id="resume-fact-final-text"
              aria-label="经确认的最终简历表述"
              value={finalText}
              disabled={frozen}
              maxLength={400}
              showCount={{ formatter: ({ value }) => `${Array.from(value).length}/400` }}
              rows={7}
              onChange={(event) => setFinalText(event.target.value)}
              placeholder="例如：负责订单接口治理，将平均响应时间从 320ms 降至 180ms（仅填写你能确认的事实）"
            />
            <Checkbox
              checked={confirmed}
              disabled={frozen}
              onChange={(event) => setConfirmed(event.target.checked)}
            >
              以上内容是本人确认的真实事实
            </Checkbox>

            {!lineageKnown && (
              <div className={styles.factOperationStatus} role="status">
                <span>关系待确认，暂不能创建新版本。</span>
              </div>
            )}

            {statusText && (
              <div
                className={`${styles.factOperationStatus} ${frozen ? styles.factOperationUnknown : ''}`}
                role="status"
              >
                {operationState === 'source_changed' && <CheckCircleFilled />}
                <span>{statusText}</span>
              </div>
            )}

            <div className={styles.factActions}>
              {operationState === 'unknown' ? (
                <Button type="primary" onClick={onExitToLibrary ?? onClose}>返回简历库核对</Button>
              ) : operationState === 'source_changed' && createdCopy ? (
                <Button type="primary" onClick={() => onContinueInCopy?.(createdCopy)}>
                  打开已创建版本重新体检
                </Button>
              ) : (
                <>
                  <Button disabled={operationState === 'saving'} onClick={onClose}>稍后处理</Button>
                  <Button
                    type="primary"
                    loading={operationState === 'saving'}
                    disabled={!canSubmit}
                    onClick={() => void handleSubmit()}
                  >
                    {createdCopy ? '继续保存并查看差异' : '创建新版本并查看差异'}
                  </Button>
                </>
              )}
            </div>
          </section>
        </div>
      </section>
    </div>
  );
}

function buildVersionTitle(source: Resume): string {
  const base = (source.title || source.name || '未命名简历').trim();
  const now = new Date();
  const stamp = [
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
  ].join('-');
  const time = [
    String(now.getHours()).padStart(2, '0'),
    String(now.getMinutes()).padStart(2, '0'),
    String(now.getSeconds()).padStart(2, '0'),
  ].join(':');
  return `${base} · 事实补充版 ${stamp} ${time}`;
}

function getFocusableElements(container: HTMLElement | null): HTMLElement[] {
  if (!container) return [];
  return Array.from(container.querySelectorAll<HTMLElement>(
    'button:not([disabled]), textarea:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
  )).filter((element) => element.offsetParent !== null || element === document.activeElement);
}
