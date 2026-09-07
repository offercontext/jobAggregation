import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Input, Modal, Progress, Space, Tag, message } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updateResume } from '@/services/resumes';
import type { Resume, ResumeContent, UpdateResumeInput } from '@/types/resume';
import { formatResumeLineage } from '@/features/materialSurfaces/materialLabels';
import { resolveResumeLineage } from '@/features/materialSurfaces/resumeLineage';
import { buildAdvancedResumeJson, parseStructuredResume, serializeStructuredResume, type StructuredResumeDraft } from '@/lib/structuredResume';
import dayjs from 'dayjs';
import styles from './ResumeLibraryView.module.css';
import ResumeEvidenceAuditPanel from './ResumeEvidenceAuditPanel';
import ResumeFactSupplementWorkspace from './ResumeFactSupplementWorkspace';
import type { ResumeAuditFinding } from '@/lib/resumeEvidenceAudit';
import ResumeImportReview from './ResumeImportReview';

interface Props {
  resume: Resume | null;
  open: boolean;
  onClose: () => void;
  onSaved?: (resume: Resume) => void;
  onFactVersionCreated?: (resume: Resume) => void;
  onFactCopyCreated?: (resume: Resume) => void;
  onFactContinueInCopy?: (resume: Resume) => void;
  onFactExitToLibrary?: () => void;
  onFactCopyResultUnknown?: () => void;
  resumes?: readonly Resume[];
}

type SectionKey = 'intent' | 'contact' | 'education' | 'experience' | 'projects' | 'skills' | 'other' | 'original';

const SECTION_LABELS: Record<string, string> = {
  career_intent: '求职意向', contact: '基本信息', education: '教育经历', experience: '工作经历', projects: '项目经历', skills: '技能',
};

const SOURCE_LABELS: Record<string, string> = {
  manual: '手动创建', dialog: 'Haru 对话', upload: '现有简历上传', sample: '样例', sample_copy: '样例副本',
};

const SECTIONS: Array<{ key: SectionKey; label: string }> = [
  { key: 'intent', label: '求职意向' },
  { key: 'contact', label: '基本信息' },
  { key: 'education', label: '教育经历' },
  { key: 'experience', label: '工作经历' },
  { key: 'projects', label: '项目经历' },
  { key: 'skills', label: '技能' },
  { key: 'other', label: '其他' },
];

const EMPTY_DRAFT: StructuredResumeDraft = {
  careerIntent: { targetRoles: [], targetLocations: [] },
  contact: {}, education: [], experience: [], projects: [], skills: [], rawText: '',
};

export default function ResumeEditorDrawer({
  resume, open, onClose, onSaved, onFactVersionCreated, onFactCopyCreated,
  onFactContinueInCopy, onFactExitToLibrary, onFactCopyResultUnknown,
  resumes,
}: Props) {
  const qc = useQueryClient();
  const parsed = useMemo(() => parseStructuredResume(resume?.source === 'upload' && resume.content_json && typeof resume.content_json === 'object' && !('raw_text' in resume.content_json)
    ? { ...resume.content_json, raw_text: resume.parsed_data } : resume?.content_json), [resume?.content_json, resume?.source, resume?.parsed_data]);
  const baselineDraft = parsed.mode === 'structured' ? parsed.draft : EMPTY_DRAFT;
  const baselineFingerprint = useMemo(() => JSON.stringify(baselineDraft), [baselineDraft]);
  const [title, setTitle] = useState('');
  const [draft, setDraft] = useState<StructuredResumeDraft>(baselineDraft);
  const [activeSection, setActiveSection] = useState<SectionKey>('intent');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedJson, setAdvancedJson] = useState('');
  const [advancedBaseline, setAdvancedBaseline] = useState('');
  const [auditOpen, setAuditOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importReviewed, setImportReviewed] = useState(false);
  const [supplementFinding, setSupplementFinding] = useState<ResumeAuditFinding | null>(null);
  const lineageResumes = useMemo(() => {
    if (!resume) return resumes ?? [];
    if (resumes?.some((candidate) => candidate.id === resume.id)) return resumes;
    return [resume, ...(resumes ?? [])];
  }, [resume, resumes]);
  const lineage = useMemo(
    () => resume ? resolveResumeLineage(lineageResumes, resume.id) : null,
    [lineageResumes, resume],
  );

  useEffect(() => {
    if (!resume) return;
    setTitle(resume.title || resume.name || '');
    setDraft(parsed.mode === 'structured' ? parsed.draft : EMPTY_DRAFT);
    setAdvancedJson(parsed.mode === 'recovery' ? parsed.raw : JSON.stringify(resume.content_json, null, 2));
    setAdvancedBaseline(parsed.mode === 'recovery' ? parsed.raw : JSON.stringify(resume.content_json, null, 2));
    setActiveSection('intent');
    setAdvancedOpen(false);
    setAuditOpen(false);
    setImportOpen(false);
    setSupplementFinding(null);
  }, [open, parsed, resume]);

  useEffect(() => {
    let current = true;
    setImportReviewed(false);
    const review = resume?.content_json?.import_review;
    if (resume?.source === 'upload' && review && typeof review === 'object' && 'version' in review && review.version === 1 && 'raw_text_sha256' in review && typeof review.raw_text_sha256 === 'string' && globalThis.crypto?.subtle) {
      const expected = review.raw_text_sha256;
      const raw = typeof resume.content_json.raw_text === 'string' ? resume.content_json.raw_text : resume.parsed_data;
      void crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw)).then((buffer) => {
        if (current) setImportReviewed(Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, '0')).join('') === expected);
      }).catch(() => { /* A status hint must not block editing. */ });
    }
    return () => { current = false; };
  }, [resume?.content_json, resume?.parsed_data, resume?.source]);

  const saveMut = useMutation({
    mutationFn: (input: UpdateResumeInput) => updateResume(resume!.id, input),
    onSuccess: (updated) => {
      message.success('已保存');
      void qc.invalidateQueries({ queryKey: ['resumes'] });
      onSaved?.(updated);
      onClose();
    },
    onError: () => message.error('保存失败'),
  });

  const missingLabels = useMemo(
    () => (resume?.missing_sections ?? []).map((item) => SECTION_LABELS[item] ?? item),
    [resume?.missing_sections],
  );
  const editorDirty = Boolean(resume) && (
    title !== (resume?.title || resume?.name || '')
    || JSON.stringify(draft) !== baselineFingerprint
    || (advancedOpen && advancedJson !== advancedBaseline)
  );

  if (!open || !resume) return null;

  const requestClose = () => {
    if (!editorDirty) {
      onClose();
      return;
    }
    Modal.confirm({
      title: '有未保存的更改',
      content: '离开后，本次编辑内容不会保存。',
      okText: '放弃更改',
      cancelText: '继续编辑',
      okButtonProps: { danger: true },
      onOk: onClose,
    });
  };

  const handleSupplement = (finding: ResumeAuditFinding) => {
    if (editorDirty) {
      message.warning('请先保存或取消当前编辑，再创建事实补充版本');
      return;
    }
    setSupplementFinding(finding);
  };

  const toggleAdvancedEditor = () => {
    if (!advancedOpen) {
      if (parsed.mode === 'structured') {
        const generated = buildAdvancedResumeJson(resume.content_json, draft);
        setAdvancedJson(generated);
        setAdvancedBaseline(generated);
      }
      setAdvancedOpen(true);
      return;
    }

    try {
      const advancedContent = JSON.parse(advancedJson) as ResumeContent;
      const nextParsed = parseStructuredResume(advancedContent);
      if (nextParsed.mode !== 'structured') throw new Error('高级 JSON 与当前简历契约不兼容');
      setDraft(nextParsed.draft);
      setAdvancedOpen(false);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '请先修复高级 JSON');
    }
  };

  const handleSave = () => {
    if (!title.trim()) {
      message.error('请填写简历标题');
      return;
    }
    try {
      let content: ResumeContent;
      if (advancedOpen) {
        content = JSON.parse(advancedJson) as ResumeContent;
        if (parseStructuredResume(content).mode !== 'structured') throw new Error('高级 JSON 与当前简历契约不兼容');
      } else {
        if (parsed.mode === 'recovery') throw new Error('当前历史数据无法安全使用结构化表单保存');
        content = serializeStructuredResume(resume.content_json, draft);
      }
      if (parseStructuredResume(content).mode !== 'structured') throw new Error('保存前 round-trip 校验失败');
      saveMut.mutate({ title: title.trim(), content_json: content, career_intent: content.career_intent });
    } catch (error) {
      message.error(error instanceof Error ? error.message : '简历内容格式错误');
    }
  };

  return (
    <section className={styles.editorWorkspace} aria-label="编辑简历">
      <div className={styles.editorWorkspaceToolbar}>
        <div>
          <Button type="link" className={styles.backButton} onClick={requestClose}>返回简历库</Button>
          <div className={styles.editorWorkspaceTitle}>编辑简历</div>
        </div>
        <Space wrap>
          <Button aria-expanded={auditOpen} aria-controls="resume-evidence-audit-panel" onClick={() => setAuditOpen((value) => !value)}>
            简历事实体检
          </Button>
          <Button aria-expanded={advancedOpen} onClick={toggleAdvancedEditor}>高级 JSON</Button>
          <Button onClick={requestClose}>取消</Button>
          <Button type="primary" disabled={parsed.mode === 'recovery' && !advancedOpen} loading={saveMut.isPending} onClick={handleSave}>保存</Button>
        </Space>
      </div>

      {resume.source === 'upload' && <Alert type="info" showIcon
        message={importReviewed ? '已核对分类' : baselineDraft.rawText.trim() ? '已提取文字，待分类' : '未提取到文字'}
        description="PDF 原文单独保留。AI 只生成待核对候选，确认后填入空白模块，不覆盖已有内容。扫描件暂不支持 OCR。"
        action={<Button disabled={parsed.mode === 'recovery' || !baselineDraft.rawText.trim() || saveMut.isPending} onClick={() => {
          if (editorDirty) { message.warning('请先保存或取消当前编辑，再进行 AI 分类'); return; }
          setImportOpen(true);
        }}>{importReviewed ? '重新解析并核对' : 'AI 分类并核对'}</Button>}
      />}

      <div className={styles.editorHeader}>
        <Input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="简历标题" className={styles.editorTitleInput} />
        <div className={styles.editorMeta}>
          <Tag color={lineage?.kind === 'base' ? 'blue' : lineage?.kind === 'relationship_unknown' ? 'warning' : 'default'}>
            {lineage ? formatResumeLineage(lineage) : '关系待确认'}
          </Tag>
          <Tag>{SOURCE_LABELS[resume.source] ?? '来源待确认'}</Tag>
          <span>{dayjs(resume.created_at).format('YYYY-MM-DD HH:mm')}</span>
        </div>
        <div className={styles.editorCompletion}>
          <Progress percent={resume.completion_percent ?? 0} size="small" />
          <div className={styles.missingLine}>
            {missingLabels.length ? <><span>待补：</span>{missingLabels.map((label) => <Tag key={label}>{label}</Tag>)}</> : <Tag color="success">结构完整</Tag>}
          </div>
        </div>
      </div>

      {parsed.mode === 'recovery' ? (
        <Alert
          type="error"
          showIcon
          message="无法安全使用结构化表单"
          description={`${parsed.reason}。原始内容保持只读；如需恢复，请复制内容后在“高级 JSON”中谨慎修复。`}
        />
      ) : null}

      {auditOpen ? <div id="resume-evidence-audit-panel"><ResumeEvidenceAuditPanel resume={resume} onSupplement={handleSupplement} /></div> : null}

      {advancedOpen ? (
        <section className={styles.sectionEditor} aria-label="高级 JSON 编辑">
          <Alert type="warning" showIcon message="高级入口会直接编辑完整 JSON；保存前仍会校验现有契约和 round-trip。" />
          <Input.TextArea className={styles.codeTextarea} rows={22} value={advancedJson} onChange={(event) => setAdvancedJson(event.target.value)} />
        </section>
      ) : parsed.mode === 'recovery' ? (
        <Input.TextArea className={styles.codeTextarea} rows={22} value={parsed.raw} readOnly aria-label="历史简历原始内容" />
      ) : (
        <div className={styles.editorGrid}>
          <nav className={styles.sectionNav} aria-label="简历章节">
            {(resume.source === 'upload' ? [...SECTIONS, { key: 'original' as const, label: 'PDF 提取原文' }] : SECTIONS).map((section) => (
              <button key={section.key} type="button" className={activeSection === section.key ? styles.sectionNavActive : undefined} onClick={() => setActiveSection(section.key)}>
                {section.label}
              </button>
            ))}
          </nav>
          <section className={styles.sectionEditor}>
            <StructuredSection section={activeSection} draft={draft} onChange={setDraft} imported={resume.source === 'upload'} />
          </section>
        </div>
      )}

      {importOpen && <ResumeImportReview key={resume.id} resume={resume} onClose={() => setImportOpen(false)} onSaved={(updated) => {
        setImportOpen(false);
        void qc.invalidateQueries({ queryKey: ['resumes'] });
        onSaved?.(updated);
        onClose();
      }} />}

      {supplementFinding ? (
        <ResumeFactSupplementWorkspace
          open source={resume} resumes={lineageResumes} finding={supplementFinding} onClose={() => setSupplementFinding(null)}
          onCompleted={(created) => { setSupplementFinding(null); onFactVersionCreated?.(created); }}
          onCopyCreated={onFactCopyCreated}
          onContinueInCopy={(copy) => { setSupplementFinding(null); onFactContinueInCopy?.(copy); }}
          onExitToLibrary={() => { setSupplementFinding(null); onFactExitToLibrary?.(); }}
          onCopyResultUnknown={onFactCopyResultUnknown}
        />
      ) : null}
    </section>
  );
}

function StructuredSection({ section, draft, onChange, imported = false }: { section: SectionKey; draft: StructuredResumeDraft; onChange: (draft: StructuredResumeDraft) => void; imported?: boolean }) {
  if (section === 'original') return <>
    <div className={styles.sectionTitle}>PDF 提取原文</div>
    <p>这是上传文件提取的文字，分类和填写“其他”不会修改它。需要重新提取时请重新上传文件。</p>
    <Input.TextArea rows={18} value={draft.rawText} readOnly aria-label="PDF 提取原文" />
  </>;
  if (section === 'intent') {
    return <>
      <div className={styles.sectionTitle}>求职意向</div>
      <LabeledInput label="目标岗位" value={draft.careerIntent.targetRoles.join(', ')} onChange={(value) => onChange({ ...draft, careerIntent: { ...draft.careerIntent, targetRoles: splitList(value) } })} />
      <LabeledInput label="目标城市" value={draft.careerIntent.targetLocations.join(', ')} onChange={(value) => onChange({ ...draft, careerIntent: { ...draft.careerIntent, targetLocations: splitList(value) } })} />
    </>;
  }
  if (section === 'contact') {
    return <>
      <div className={styles.sectionTitle}>基本信息</div>
      {(['name', 'email', 'phone', 'location'] as const).map((field) => (
        <LabeledInput key={field} label={{ name: '姓名', email: '邮箱', phone: '电话', location: '所在地' }[field]} value={stringValue(draft.contact[field])} onChange={(value) => onChange({ ...draft, contact: { ...draft.contact, [field]: value } })} />
      ))}
    </>;
  }
  if (section === 'skills') {
    return <>
      <div className={styles.sectionTitle}>技能</div>
      <Input.TextArea rows={8} value={draft.skills.join('\n')} placeholder="每行一个技能" onChange={(event) => onChange({ ...draft, skills: event.target.value.split('\n').map((item) => item.trim()).filter(Boolean) })} />
    </>;
  }
  if (section === 'other') {
    return <>
      <div className={styles.sectionTitle}>其他</div>
      <Input.TextArea rows={14} value={imported ? draft.additionalText ?? '' : draft.rawText} placeholder="补充现有简历中的其他文本" onChange={(event) => onChange(imported ? { ...draft, additionalText: event.target.value } : { ...draft, rawText: event.target.value })} />
    </>;
  }
  const meta = {
    education: { title: '教育经历', fields: [['school', '学校'], ['degree', '学历'], ['major', '专业'], ['start_date', '开始时间'], ['end_date', '结束时间']] },
    experience: { title: '工作经历', fields: [['company', '公司'], ['title', '职位'], ['start_date', '开始时间'], ['end_date', '结束时间'], ['highlights', '工作亮点']] },
    projects: { title: '项目经历', fields: [['name', '项目名称'], ['role', '职责'], ['start_date', '开始时间'], ['end_date', '结束时间'], ['highlights', '项目亮点']] },
  }[section];
  const entries = draft[section];
  return <EntrySection
    title={meta.title}
    entries={entries}
    fields={meta.fields}
    onChange={(next) => onChange({ ...draft, [section]: next })}
  />;
}

function EntrySection({ title, entries, fields, onChange }: { title: string; entries: Record<string, unknown>[]; fields: string[][]; onChange: (entries: Record<string, unknown>[]) => void }) {
  const move = (index: number, offset: number) => {
    const target = index + offset;
    if (target < 0 || target >= entries.length) return;
    const next = [...entries];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };
  return <>
    <div className={styles.sectionTitle}>{title}</div>
    <div className={styles.structuredEntryList}>
      {entries.map((entry, index) => (
        <div className={styles.structuredEntry} key={`${index}-${stringValue(entry[fields[0][0]])}`}>
          <div className={styles.structuredEntryToolbar}>
            <strong>{title} {index + 1}</strong>
            <Space size="small">
              <Button size="small" disabled={index === 0} onClick={() => move(index, -1)}>上移</Button>
              <Button size="small" disabled={index === entries.length - 1} onClick={() => move(index, 1)}>下移</Button>
              <Button size="small" danger onClick={() => onChange(entries.filter((_, itemIndex) => itemIndex !== index))}>删除</Button>
            </Space>
          </div>
          {fields.map(([field, label]) => field === 'highlights' ? (
            <div key={field}><label className={styles.fieldLabel}>{label}</label><Input.TextArea rows={4} value={arrayText(entry[field])} onChange={(event) => onChange(entries.map((item, itemIndex) => itemIndex === index ? { ...item, [field]: event.target.value.split('\n').filter(Boolean) } : item))} /></div>
          ) : <LabeledInput key={field} label={label} value={stringValue(entry[field])} onChange={(value) => onChange(entries.map((item, itemIndex) => itemIndex === index ? { ...item, [field]: value } : item))} />)}
        </div>
      ))}
    </div>
    <Button onClick={() => onChange([...entries, {}])}>新增{title}</Button>
  </>;
}

function LabeledInput({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <div><label className={styles.fieldLabel}>{label}</label><Input value={value} status={!value.trim() && ['姓名', '学校', '公司', '项目名称'].includes(label) ? 'warning' : undefined} onChange={(event) => onChange(event.target.value)} /></div>;
}

function splitList(value: string) { return value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean); }
function stringValue(value: unknown) { return typeof value === 'string' ? value : ''; }
function arrayText(value: unknown) { return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string').join('\n') : ''; }
