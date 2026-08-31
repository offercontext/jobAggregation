import { useEffect, useMemo, useRef, useState } from 'react';
import { isSafeReadinessFeedbackItem, type ReadinessFeedbackItem, type ReadinessPracticeLaunch } from './contracts';
import { getEventReadinessFeedback } from './service';
import styles from './reviewReadiness.module.css';

interface ReadinessFeedbackAdvisoryProps {
  applicationId?: number;
  eventId: number;
  initialItems?: readonly ReadinessFeedbackItem[];
  selectedVersionIds: readonly number[];
  onSelectionChange: (versionIds: number[]) => void;
  onContractReady?: () => void;
  onOpenPractice?: (launch: ReadinessPracticeLaunch) => void;
  ownerGeneration?: number;
  frozen?: boolean;
}

const MAX_SELECTED = 8;

function stateCopy(item: ReadinessFeedbackItem): string {
  if (item.state === 'stale_source') return '来源已变化，不可选';
  if (item.state === 'retracted') return '已撤销，不可选';
  if (item.state === 'unavailable') return '暂不可用';
  if (item.state === 'practiced') return '已完成对应练习';
  if (item.practiceState === 'in_progress') return '练习进行中';
  return '可用于本次准备';
}

export function ReadinessFeedbackAdvisory({
  applicationId,
  eventId,
  initialItems,
  selectedVersionIds,
  onSelectionChange,
  onContractReady,
  onOpenPractice,
  ownerGeneration = 1,
  frozen = false,
}: ReadinessFeedbackAdvisoryProps) {
  const [items, setItems] = useState<ReadinessFeedbackItem[] | null>(initialItems ? [...initialItems] : null);
  const [loading, setLoading] = useState(!initialItems);
  const [error, setError] = useState(false);
  const requestGeneration = useRef(0);

  const load = async () => {
    if (initialItems) {
      setItems([...initialItems]);
      setLoading(false);
      setError(false);
      onContractReady?.();
      return;
    }
    if (!applicationId) {
      setLoading(false);
      setError(true);
      return;
    }
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
    setLoading(true);
    setError(false);
    try {
      const response = await getEventReadinessFeedback(applicationId, eventId);
      if (requestGeneration.current !== generation) return;
      setItems(response.items);
      onContractReady?.();
    } catch {
      if (requestGeneration.current !== generation) return;
      setError(true);
    } finally {
      if (requestGeneration.current === generation) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    return () => { requestGeneration.current += 1; };
  }, [applicationId, eventId, initialItems]);

  const selected = useMemo(() => new Set(selectedVersionIds), [selectedVersionIds]);

  function toggle(item: ReadinessFeedbackItem) {
    if (frozen || !isSafeReadinessFeedbackItem(item)) return;
    if (selected.has(item.versionId)) {
      onSelectionChange(selectedVersionIds.filter((id) => id !== item.versionId));
      return;
    }
    if (selectedVersionIds.length >= MAX_SELECTED) return;
    onSelectionChange([...selectedVersionIds, item.versionId]);
  }

  return (
    <section className={styles.advisory} aria-label="已确认的复盘准备重点" aria-busy={loading}>
      <div className={styles.sectionHeader}>
        <div>
          <h3>复盘准备重点</h3>
          <p>默认不选择。只有当前、可用的重点会进入本次准备快照。</p>
        </div>
        <span className={styles.count}>{selectedVersionIds.length}/{MAX_SELECTED}</span>
      </div>
      {loading ? <div className={styles.skeleton} aria-hidden="true" /> : null}
      {error ? (
        <div className={styles.errorPanel} role="alert">
          <span>暂时无法读取准备重点，已保留当前选择。</span>
          <button className={styles.secondaryButton} type="button" onClick={() => void load()}>重新读取</button>
        </div>
      ) : null}
      {!loading && !error && items?.length === 0 ? <p className={styles.empty}>当前面试没有可用的已确认准备重点。</p> : null}
      {!error && items ? (
        <div className={styles.advisoryList}>
          {items.map((item) => {
            const disabled = frozen || !isSafeReadinessFeedbackItem(item);
            return (
              <article key={item.versionId} className={styles.advisoryItem} data-state={item.state}>
                <label>
                  <input
                    type="checkbox"
                    checked={selected.has(item.versionId)}
                    disabled={disabled || (!selected.has(item.versionId) && selectedVersionIds.length >= MAX_SELECTED)}
                    onChange={() => toggle(item)}
                  />
                  <span><strong>{item.title}</strong><small>{item.sourceLabel}</small></span>
                </label>
                <span className={styles.stateLabel}>{stateCopy(item)}</span>
                {onOpenPractice && item.state === 'available' && item.practiceState === 'not_started' ? (
                  <button className={styles.inlineButton} type="button" disabled={frozen} onClick={() => onOpenPractice({ ownerGeneration, signalVersionId: item.versionId, targetEventId: eventId })}>用这个重点练习</button>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : null}
      {selectedVersionIds.length >= MAX_SELECTED ? <p className={styles.limit} role="status">最多选择 8 个准备重点。</p> : null}
    </section>
  );
}

export default ReadinessFeedbackAdvisory;
