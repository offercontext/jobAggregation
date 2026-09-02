import { useEffect, useState } from 'react';
import { Button, Checkbox, Input } from 'antd';
import { OFFER_STATUS_LABELS, type OfferNegotiationBlock } from '@/types/offer';
import styles from './OfferNegotiationPresentation.module.css';

type ProposalKind = 'communication_goals' | 'clarification_questions' | 'talking_points' | 'preparation_checks';

interface Props {
  kind?: ProposalKind;
  block: OfferNegotiationBlock;
  selected: boolean;
  editedText: string;
  disabled: boolean;
  onToggle: () => void;
  onEdit: (text: string) => void;
}

function evidenceLabel(path: string): string {
  if (path.endsWith('/company_name')) return '公司';
  if (path.endsWith('/position_name')) return '职位';
  if (path.endsWith('/base_monthly')) return '当前固定月薪';
  if (path.endsWith('/months_per_year')) return '计薪月数';
  if (path.endsWith('/signing_bonus')) return '签字费';
  if (path.endsWith('/equity')) return '股权记录';
  if (path.endsWith('/perks')) return '福利记录';
  if (path.endsWith('/deadline')) return '决策截止时间';
  if (path.endsWith('/notes')) return 'Offer 备注';
  if (path.endsWith('/status')) return '当前 Offer 状态';
  if (path.endsWith('/goal')) return '你的谈薪目标';
  if (path.endsWith('/concerns')) return '你的顾虑';
  if (path.endsWith('/scenario')) return '沟通场景';
  if (/\/dimensions\/dimension_[0-9]{3}\/value_text$/.test(path)) return '自定义比较项';
  return '相关依据';
}

function evidenceExcerpt(path: string, excerpt: string): string {
  if (path.endsWith('/status')) {
    const label = OFFER_STATUS_LABELS[excerpt as keyof typeof OFFER_STATUS_LABELS];
    if (label) return label;
  }
  if (path.endsWith('/base_monthly')) {
    const value = Number(excerpt);
    if (Number.isSafeInteger(value)) return `¥${value.toLocaleString('zh-CN')}/月`;
  }
  if (path.endsWith('/months_per_year')) {
    const value = Number(excerpt);
    if (Number.isSafeInteger(value)) return `${value} 薪`;
  }
  if (path.endsWith('/signing_bonus')) {
    const value = Number(excerpt);
    if (Number.isSafeInteger(value)) return `¥${value.toLocaleString('zh-CN')}`;
  }
  return excerpt;
}

export default function NegotiationProposalCard({ kind, block, selected, editedText, disabled, onToggle, onEdit }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');

  useEffect(() => {
    setCopyState('idle');
  }, [block.id, editedText]);

  const copyTalkingPoint = async () => {
    try {
      if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(editedText);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  };

  return (
    <article className={`${styles.proposalCard} ${selected ? styles.proposalCardSelected : ''}`} data-selected={selected}>
      <div className={styles.proposalHeader}>
        <span>{disabled ? '可直接使用' : '可直接使用或编辑'}</span>
        {kind === 'talking_points' && (
          <Button size="small" type="text" aria-label="复制这段表达" onClick={() => void copyTalkingPoint()}>
            {copyState === 'copied' ? '已复制' : '复制表达'}
          </Button>
        )}
      </div>
      <div className={styles.proposalMain}>
        <Checkbox checked={selected} disabled={disabled} onChange={onToggle}>
          <span className={styles.proposalText}>{block.text}</span>
        </Checkbox>
      </div>
      <p className={styles.rationale}><strong>这样安排的原因：</strong>{block.rationale}</p>
      {selected && <Input.TextArea aria-label="编辑谈薪建议" value={editedText} disabled={disabled} onChange={(event) => onEdit(event.target.value)} autoSize={{ minRows: 2, maxRows: 5 }} />}
      <button type="button" className={styles.evidenceToggle} data-action="toggle-evidence" aria-expanded={expanded} onClick={() => setExpanded((current) => !current)}>
        {expanded ? '收起依据' : '查看依据'}
      </button>
      {expanded && (
        <dl className={styles.evidenceDetails}>
          {block.evidence_refs.map((ref) => (
            <div key={`${ref.source}-${ref.path}`}>
              <dt>{evidenceLabel(ref.path)}</dt>
              <dd>
                <span>{evidenceExcerpt(ref.path, ref.excerpt)}</span>
                <small className={styles.sourcePath}>来源路径 <code>{ref.path}</code></small>
              </dd>
            </div>
          ))}
        </dl>
      )}
      <span className={styles.copyStatus} aria-live="polite">
        {copyState === 'copied' ? '表达已复制。' : copyState === 'failed' ? '复制失败，请手动选择文本。' : ''}
      </span>
    </article>
  );
}
