import { useMemo, useState } from 'react';
import type { InterviewStorySourceCandidates, InterviewStorySourceSelection } from '@/types/interviewStory';
import { STORY_SOURCE_KINDS, storySelectionKey, storySourceGroups, type StoryDisplayLeaf } from '@/lib/storySourcePresentation';
import styles from './StorySourcePicker.module.css';

interface Props { candidates: InterviewStorySourceCandidates; selections: InterviewStorySourceSelection[]; frozen: boolean; onToggle: (selection: InterviewStorySourceSelection) => void }
function SourceLeaf({ leaf, checked, frozen, onToggle }: { leaf: StoryDisplayLeaf; checked: boolean; frozen: boolean; onToggle: Props['onToggle'] }) {
  const [expanded, setExpanded] = useState(false);
  return <div className={styles.leaf}>
    <label className={styles.check}><input type="checkbox" checked={checked} disabled={frozen} onChange={() => { if (!frozen) onToggle(leaf.selection); }} /><span>{leaf.label}</span></label>
    <p className={`${styles.preview} ${expanded ? '' : styles.clamped}`}>{leaf.preview}</p>
    {leaf.preview.length > 100 || leaf.preview.includes('\n') ? <button type="button" className={styles.link} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起原文预览' : '展开原文预览'}</button> : null}
  </div>;
}
export default function StorySourcePicker({ candidates, selections, frozen, onToggle }: Props) {
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('all');
  const [selectedOnly, setSelectedOnly] = useState(false);
  const groups = useMemo(() => storySourceGroups(candidates), [candidates]);
  const selected = new Set(selections.map(storySelectionKey));
  const visible = groups.filter((group) => (kind === 'all' || group.kind === kind) && (selectedOnly
    ? group.sections.some((s) => s.leaves.some((leaf) => selected.has(storySelectionKey(leaf.selection))))
    : `${group.title} ${group.sections.flatMap((s) => s.leaves.map((l) => `${l.label} ${l.preview}`)).join(' ')}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())));
  return <section aria-label="参考材料" className={styles.picker}>
    <label className={styles.search}>查找材料<input type="search" value={query} placeholder="搜索名称或内容" onChange={(e) => { setQuery(e.target.value); setSelectedOnly(false); }} /></label>
    <div className={styles.filters} aria-label="材料类别">
      {[['all', '全部'], ...Object.entries(STORY_SOURCE_KINDS)].map(([value, label]) => <button type="button" key={value} aria-pressed={kind === value && !selectedOnly} onClick={() => { setKind(value); setSelectedOnly(false); }}>{label}</button>)}
      <button type="button" aria-pressed={selectedOnly} onClick={() => { setSelectedOnly(!selectedOnly); setKind('all'); setQuery(''); }}>只看已选（{selections.length}）</button>
    </div>
    <p className={styles.hint}>展开材料后，勾选与这次故事有关的内容。搜索和展开不会自动选择。</p>
    <p className={styles.hint}>以下为原文预览，较长来源可能已截短；整理时仍按所选片段定位原始内容。</p>
    {!visible.length ? <p role="status" className={styles.empty}>{groups.length ? '没有匹配的材料，请换个关键词或类别。已选内容仍保留。' : '暂无参考材料。你也可以在下方补充自己的经历。'}</p> : null}
    {visible.map((group) => {
      const leaves = group.sections.flatMap((s) => s.leaves);
      const count = leaves.filter((leaf) => selected.has(storySelectionKey(leaf.selection))).length;
      return <details key={group.key} className={styles.material}>
        <summary><span className={styles.category}>{STORY_SOURCE_KINDS[group.kind]}</span><strong>{group.title}</strong><span className={styles.count}>{count ? `已选${count}项` : `${leaves.length}项可选内容`}</span><span className={styles.summaryPreview}>{leaves[0]?.preview.slice(0, 90) || '展开查看内容'}</span></summary>
        <div className={styles.contents}>{group.sections.filter((section) => !selectedOnly || section.leaves.some((leaf) => selected.has(storySelectionKey(leaf.selection)))).map((section) => <section key={section.key} className={styles.turn}>
          {section.title && <h4>{section.title}</h4>}
          {section.leaves.filter((leaf) => !selectedOnly || selected.has(storySelectionKey(leaf.selection))).map((leaf) => <SourceLeaf key={storySelectionKey(leaf.selection)} leaf={leaf} checked={selected.has(storySelectionKey(leaf.selection))} frozen={frozen} onToggle={onToggle} />)}
        </section>)}</div>
      </details>;
    })}
  </section>;
}
