import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Checkbox, Input, Modal, Space, Typography } from 'antd';
import { confirmResumeStructure, getResume, previewResumeStructure } from '@/services/resumes';
import type { Resume, ResumeImportField, ResumeImportPreview } from '@/types/resume';
import { importFieldLabel, isImportFieldProtected, reindexImportFields } from '@/lib/resumeImport';

interface Props { resume: Resume; onSaved: (resume: Resume) => void; onClose: () => void }
type Phase = 'idle' | 'generating' | 'review' | 'saving' | 'unknown' | 'refreshing' | 'changed';
function sourceIdentity(resume: Resume): string {
  return JSON.stringify([resume.id, resume.source, resume.source_file_path, resume.parse_status, resume.parsed_data, resume.content_json, resume.deleted_at]);
}
function failureText(error: unknown): string {
  const code = (error as { response?: { data?: { code?: string; error_code?: string } } })?.response?.data;
  const value = code?.code ?? code?.error_code ?? '';
  if (value.includes('empty_source')) return '没有可分类的原文。请检查 PDF 是否为扫描件，或重新上传。';
  if (value.includes('budget') || value.includes('limit') || value.includes('large')) return '简历内容超出本次分类限额，请检查 AI 上下文配置或缩短文件。原文仍然保留。';
  if (value.includes('configured') || value.includes('configuration')) return 'AI 尚未配置或配置不可用，请检查 AI 设置。原文仍然保留。';
  return '分类未完成，原文仍然保留。请检查 AI 设置或稍后主动重试。';
}

export default function ResumeImportReview({ resume, onSaved, onClose }: Props) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [preview, setPreview] = useState<ResumeImportPreview | null>(null);
  const [fields, setFields] = useState<ResumeImportField[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [latestRead, setLatestRead] = useState<Resume | null>(null);
  const request = useRef<{ generation: number; controller: AbortController | null; busy: boolean }>({ generation: 0, controller: null, busy: false });
  const source = sourceIdentity(resume);
  const alertRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const state = request.current;
    state.generation += 1; state.controller?.abort(); state.busy = false;
    setPhase('idle'); setPreview(null); setFields([]); setSelected(new Set()); setError(''); setNotice(''); setLatestRead(null);
    return () => { state.generation += 1; state.controller?.abort(); state.busy = false; };
  }, [resume.id, source]);
  useEffect(() => { if (error) alertRef.current?.focus(); }, [error]);
  const begin = () => {
    if (request.current.busy) return null;
    const controller = new AbortController();
    request.current.controller = controller; request.current.busy = true;
    const generation = ++request.current.generation;
    return { signal: controller.signal, valid: () => request.current.generation === generation && !controller.signal.aborted };
  };
  const generate = async () => {
    const attempt = begin(); if (!attempt) return;
    setPhase('generating'); setError('');
    try {
      const result = await previewResumeStructure(resume.id, attempt.signal);
      if (!attempt.valid()) return;
      if (result.resume_id !== resume.id || !result.source_fingerprint || !Array.isArray(result.fields) || result.fields.length > 1000 || result.fields.some((f) => typeof f.path !== 'string' || typeof f.value !== 'string' || typeof f.evidence !== 'string')) throw new Error('invalid preview');
      setPreview(result); setFields(result.fields.map((f) => ({ ...f })));
      setSelected(new Set(result.fields.filter((f) => !isImportFieldProtected(resume.content_json, f.path)).map((f) => f.path)));
      setPhase('review');
    } catch (failure) { if (attempt.valid()) { setError(failureText(failure)); setPhase('idle'); } }
    finally { if (attempt.valid()) request.current.busy = false; }
  };
  const chosen = fields.filter((f) => selected.has(f.path) && !isImportFieldProtected(resume.content_json, f.path));
  const confirm = async () => {
    if (!preview || !chosen.length) return;
    const attempt = begin(); if (!attempt) return;
    setPhase('saving'); setError(''); setNotice('');
    try {
      const updated = await confirmResumeStructure(resume.id, { source_fingerprint: preview.source_fingerprint, fields: reindexImportFields(chosen) }, attempt.signal);
      if (attempt.valid()) {
        if (updated.id !== resume.id) throw new Error('identity mismatch');
        onSaved(updated);
      }
    } catch (failure) { if (attempt.valid()) {
      const status = (failure as { response?: { status?: number } })?.response?.status;
      if (status === 422) {
        setPhase('review'); setError('候选未保存：请核对字段，确保所选文字是下方原文依据中的准确表述，或取消勾选该字段后再确认。');
      } else {
        setPhase('unknown'); setError('未能确认保存结果，或简历已发生变化。候选仍保留；请先重新读取简历，核对是否已保存。不会自动重复分类或写入。');
      }
    } }
    finally { if (attempt.valid()) request.current.busy = false; }
  };
  const refresh = async () => {
    const attempt = begin(); if (!attempt) return;
    setPhase('refreshing');
    try {
      const updated = await getResume(resume.id, attempt.signal);
      if (attempt.valid()) {
        if (updated.id !== resume.id) throw new Error('identity mismatch');
        if (sourceIdentity(updated) === source) {
          setPhase('review'); setError(''); setNotice('已重新读取：简历未发生变化，候选仍保留。请核对后再确认，不会自动重试保存。');
        } else {
          setLatestRead(updated); setPhase('changed'); setNotice(''); setError('来源或内容已变化，旧候选仍保留供核对，但不能再提交。请结束核对后查看最新简历；不会重新调用 AI。');
        }
      }
    }
    catch { if (attempt.valid()) { setPhase('unknown'); setError('读取失败，候选仍保留。请稍后重新读取；不要重复提交旧分类结果。'); } }
    finally { if (attempt.valid()) request.current.busy = false; }
  };
  const saving = phase === 'saving' || phase === 'refreshing';
  const cancel = () => { if (saving) return; request.current.generation += 1; request.current.controller?.abort(); request.current.busy = false; if (latestRead) onSaved(latestRead); else onClose(); };
  return <Modal open title="AI 简历分类与核对" width={860} styles={{ body: { maxHeight: '60vh', overflowY: 'auto', paddingRight: 8 } }} onCancel={cancel} closable={!saving} maskClosable={false} footer={<Space wrap>
    <Button onClick={cancel} disabled={saving}>取消</Button>
    {phase === 'idle' || phase === 'generating' ? <Button key="generate" type="primary" loading={phase === 'generating'} onClick={() => void generate()}>开始分类</Button> : phase === 'unknown' || phase === 'refreshing' ? <Button key="refresh" type="primary" loading={phase === 'refreshing'} onClick={() => void refresh()}>重新读取简历</Button> : phase === 'changed' ? <Button key="finish" type="primary" onClick={cancel}>结束核对并返回</Button> : <Button key="confirm" type="primary" loading={phase === 'saving'} disabled={!chosen.length || chosen.some((f) => !f.value.trim())} onClick={() => void confirm()}>确认填入空白模块</Button>}
  </Space>}>
    <Alert type="info" showIcon message="仅分类原文，不补写经历" description="点击开始后，提取的简历文字会发送给当前配置的 AI。结果只是待核对候选，确认前不会修改简历；已有内容将保留。" />
    {error && <div ref={alertRef} tabIndex={-1} role="alert" style={{ marginTop: 12 }}><Alert type="error" showIcon message={error} /></div>}
    {notice && <div role="status" style={{ marginTop: 12 }}><Alert type="info" showIcon message={notice} /></div>}
    {phase === 'generating' && <p role="status">正在分类。原文与已保存内容保持不变。</p>}
    {preview && <section aria-label="分类候选" style={{ marginTop: 16 }}>
      <Typography.Title level={5}>分类完成，待核对（尚未保存）</Typography.Title>
      {!fields.length && <p>没有识别出可填入的字段，请保留原文并手动整理。</p>}
      {fields.map((field, index) => {
        const protectedField = isImportFieldProtected(resume.content_json, field.path);
        const label = importFieldLabel(field.path);
        return <div key={field.path} style={{ marginBottom: 16 }}>
          <Checkbox checked={selected.has(field.path)} disabled={protectedField || phase !== 'review'} onChange={(event) => setSelected((old) => { const next = new Set(old); if (event.target.checked) next.add(field.path); else next.delete(field.path); return next; })}>{label}{protectedField ? ' · 已有内容将保留' : ''}</Checkbox>
          <Input.TextArea aria-label={`${label}候选`} autoSize={{ minRows: 1, maxRows: 5 }} value={field.value} disabled={protectedField || phase !== 'review'} maxLength={8192} onChange={(event) => setFields((old) => old.map((f, i) => i === index ? { ...f, value: event.target.value } : f))} />
          <Typography.Paragraph type="secondary" style={{ marginTop: 4, whiteSpace: 'pre-wrap' }}>原文依据：{field.evidence}</Typography.Paragraph>
        </div>;
      })}
      <p>可取消勾选或改为原文中的准确表述。已有经历和技能列表不追加、不覆盖；其他补充请使用原编辑器。</p>
    </section>}
  </Modal>;
}
