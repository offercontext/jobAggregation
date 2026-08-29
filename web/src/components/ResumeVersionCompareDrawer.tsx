import { useEffect, useMemo, useRef, useState } from 'react';
import type { Resume } from '@/types/resume';
import { diffResumeContent, type DiffModule, type DiffText, type ResumeDiffItem } from '@/lib/resumeVersionDiff';
import { formatResumeLineage, resumeDisplayTitle } from '@/features/materialSurfaces/materialLabels';
import { resolveResumeLineage } from '@/features/materialSurfaces/resumeLineage';
import styles from './ResumeLibraryView.module.css';

export type ResumeVersionCompareDrawerProps = {
  open: boolean;
  target: Resume;
  candidates: Resume[];
  onClose: () => void;
};

const MODULE_LABELS: Record<DiffModule, string> = {
  contact: '联系方式',
  education: '教育经历',
  experience: '工作经历',
  projects: '项目经历',
  skills: '技能',
  career_intent: '求职意向',
  other: '其他结构化字段',
};

const KIND_LABELS = {
  added: '新增',
  removed: '删除',
  changed: '修改',
} as const;

export default function ResumeVersionCompareDrawer({
  open,
  target,
  candidates,
  onClose,
}: ResumeVersionCompareDrawerProps) {
  const [baselineId, setBaselineId] = useState<number | null>(null);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set());
  const previousOpen = useRef(false);
  const previousTargetId = useRef<number | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const sortedCandidates = useMemo(() => sortCandidates(target, candidates), [target, candidates]);
  const baseline = baselineId === null
    ? undefined
    : candidates.find((candidate) => candidate.id === baselineId && candidate.id !== target.id);
  const diff = baseline ? diffResumeContent(baseline.content_json, target.content_json) : null;

  useEffect(() => {
    const openedNow = open && !previousOpen.current;
    const targetChanged = open && previousTargetId.current !== target.id;
    if (open && (openedNow || targetChanged)) {
      const lineage = resolveResumeLineage(candidates, target.id);
      setBaselineId(lineage.kind === 'job_variant' ? lineage.parent?.id ?? null : null);
      setExpandedKeys(new Set());
    }
    previousOpen.current = open;
    if (open) previousTargetId.current = target.id;
  }, [candidates, open, target]);

  useEffect(() => {
    if (baselineId !== null && !candidates.some((candidate) => candidate.id === baselineId && candidate.id !== target.id)) {
      setBaselineId(null);
      setExpandedKeys(new Set());
    }
  }, [baselineId, candidates, target.id]);

  useEffect(() => {
    if (!open) return undefined;

    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    dialog?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;

      const activeDialog = dialogRef.current;
      const focusable = getFocusableElements(activeDialog);
      if (!activeDialog || focusable.length === 0) {
        event.preventDefault();
        activeDialog?.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (active === activeDialog) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (!activeDialog.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      if (opener?.isConnected) opener.focus();
    };
  }, [open]);

  if (!open) return null;

  const groupedItems = groupItems(diff?.items ?? []);

  return (
    <div className={styles.compareBackdrop} data-resume-version-compare>
      <aside ref={dialogRef} tabIndex={-1} className={styles.compareDrawer} role="dialog" aria-modal="true" aria-label="简历版本对比">
        <div className={styles.compareHeader}>
          <div>
            <div className={styles.compareEyebrow}>已保存内容审阅</div>
            <h2 className={styles.compareTitle}>对比版本</h2>
            <p className={styles.compareSubtitle}>
              当前目标：{resumeDisplayTitle(target)}
            </p>
          </div>
          <button type="button" className={styles.compareClose} aria-label="关闭版本对比" onClick={onClose}>×</button>
        </div>

        <div className={styles.compareBody}>
          <p className={styles.compareNotice}>仅比较当前已保存的简历内容，不读取编辑器中尚未保存的草稿。</p>
          <label className={styles.compareLabel}>
            基准版本
            <select
              aria-label="基准版本"
              className={styles.compareSelect}
              value={baselineId === null ? '' : String(baselineId)}
              onChange={(event) => {
                const next = event.target.value;
                setBaselineId(next ? Number(next) : null);
                setExpandedKeys(new Set());
              }}
            >
              <option value="">
                {sortedCandidates.length ? '请选择基准版本' : '暂无可比较版本'}
              </option>
              {sortedCandidates.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidateLabel(candidate, candidates)}
                </option>
              ))}
            </select>
          </label>

          {baseline && (
            <div className={styles.compareBaselineSummary}>
              基准：{resumeDisplayTitle(baseline)}
            </div>
          )}

          {!baseline ? (
            <div className={styles.compareEmptyState}>请选择一个基准版本</div>
          ) : diff?.identical ? (
            <div className={styles.compareEmptyState}>暂无差异</div>
          ) : (
            <>
              <div className={styles.compareSummary} aria-label="差异摘要">
                <span>新增 {diff?.counts.added ?? 0}</span>
                <span>删除 {diff?.counts.removed ?? 0}</span>
                <span>修改 {diff?.counts.changed ?? 0}</span>
              </div>
              <p className={styles.compareArrayNote}>数组按位置比较，不推断经历、公司或项目是否为同一项。</p>
              <div className={styles.compareGroups}>
                {Object.entries(groupedItems).map(([module, items]) => (
                  <section key={module} className={styles.compareGroup}>
                    <h3>{MODULE_LABELS[module as DiffModule]}</h3>
                    <div className={styles.compareItems}>
                      {items.map((item) => (
                        <DiffItemView
                          key={`${item.kind}:${item.path}`}
                          item={item}
                          resetKey={`${target.id}:${baselineId ?? ''}`}
                          expandedKeys={expandedKeys}
                          onToggle={(key, expanded) => {
                            setExpandedKeys((previous) => {
                              const next = new Set(previous);
                              if (expanded) next.add(key);
                              else next.delete(key);
                              return next;
                            });
                          }}
                        />
                      ))}
                    </div>
                  </section>
                ))}
              </div>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

function DiffItemView({
  item,
  resetKey,
  expandedKeys,
  onToggle,
}: {
  item: ResumeDiffItem;
  resetKey: string;
  expandedKeys: Set<string>;
  onToggle: (key: string, expanded: boolean) => void;
}) {
  return (
    <article className={styles.compareItem} data-diff-module={item.module}>
      <div className={styles.compareItemMeta}>
        <span className={styles.compareKind}>{KIND_LABELS[item.kind]}</span>
        <span>{MODULE_LABELS[item.module]}</span>
      </div>
      <div className={styles.compareValues}>
        {item.kind !== 'added' && (
          <ValueView label="修改前" value={item.before} keyPrefix={`${item.path}:before`} resetKey={resetKey} expandedKeys={expandedKeys} onToggle={onToggle} />
        )}
        {item.kind !== 'removed' && (
          <ValueView label="修改后" value={item.after} keyPrefix={`${item.path}:after`} resetKey={resetKey} expandedKeys={expandedKeys} onToggle={onToggle} />
        )}
      </div>
    </article>
  );
}

function ValueView({
  label,
  value,
  keyPrefix,
  resetKey,
  expandedKeys,
  onToggle,
}: {
  label: string;
  value: ResumeDiffItem['before'];
  keyPrefix: string;
  resetKey: string;
  expandedKeys: Set<string>;
  onToggle: (key: string, expanded: boolean) => void;
}) {
  if (!value) {
    return <div className={styles.compareValue}><span className={styles.compareValueLabel}>{label}</span><span className={styles.compareMissing}>不存在</span></div>;
  }
  const text: DiffText = value.text;
  const isExpanded = expandedKeys.has(keyPrefix);
  return (
    <div className={styles.compareValue}>
      <span className={styles.compareValueLabel}>{label}</span>
      {text.truncated ? (
        <>
          {!isExpanded && <pre>{text.preview}</pre>}
          <details key={`${resetKey}:${keyPrefix}`} open={isExpanded} onToggle={(event) => onToggle(keyPrefix, event.currentTarget.open)}>
            <summary>展开完整内容</summary>
            {isExpanded && <pre>{text.full}</pre>}
          </details>
        </>
      ) : (
        <pre>{text.preview}</pre>
      )}
    </div>
  );
}

function sortCandidates(target: Resume, candidates: Resume[]) {
  const available = candidates.filter((candidate) => (
    candidate.id !== target.id
    && isVisibleCandidate(candidate)
  ));
  const lineage = resolveResumeLineage(candidates, target.id);
  const parentId = lineage.kind === 'job_variant' ? lineage.parent?.id ?? null : null;
  return available.sort((left, right) => {
    const leftIsParent = left.id === parentId;
    const rightIsParent = right.id === parentId;
    if (leftIsParent !== rightIsParent) return leftIsParent ? -1 : 1;
    return right.id - left.id;
  });
}

function candidateLabel(candidate: Resume, candidates: Resume[]) {
  const lineage = resolveResumeLineage(candidates, candidate.id);
  return `${resumeDisplayTitle(candidate)}（${formatResumeLineage(lineage)}）`;
}

function isVisibleCandidate(candidate: Resume): boolean {
  const visibility = candidate as Resume & {
    readonly deleted?: boolean;
    readonly hidden?: boolean;
    readonly visible?: boolean;
  };
  try {
    return visibility.deleted_at == null
      && visibility.deleted !== true
      && visibility.hidden !== true
      && visibility.visible !== false;
  } catch {
    return false;
  }
}

function groupItems(items: ResumeDiffItem[]) {
  return items.reduce<Record<string, ResumeDiffItem[]>>((groups, item) => {
    (groups[item.module] ??= []).push(item);
    return groups;
  }, {});
}

function getFocusableElements(dialog: HTMLElement | null) {
  if (!dialog) return [];
  return Array.from(dialog.querySelectorAll<HTMLElement>(
    'button:not([disabled]), select:not([disabled]), textarea:not([disabled]), input:not([disabled]), a[href], summary, [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hidden && element.getAttribute('aria-hidden') !== 'true');
}
