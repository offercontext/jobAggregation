import { useEffect, useRef, useState } from 'react';
import { Button, Dropdown, Input, Spin, message } from 'antd';
import {
  CloudUploadOutlined,
  FileAddOutlined,
  FileTextOutlined,
  PlusOutlined,
  MoreOutlined,
} from '@ant-design/icons';
import type { DragEvent } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  copyResume,
  createResume,
  createResumeFromSample,
  deleteResume,
  listResumes,
  updateResume,
  uploadResume,
} from '@/services/resumes';
import { ONBOARDING_QUERY_KEY } from '@/services/onboarding';
import ResumeCard from './ResumeCard';
import ResumeUploadModal from './ResumeUploadModal';
import ResumeEditorDrawer from './ResumeEditorDrawer';
import ResumeVersionCompareDrawer from './ResumeVersionCompareDrawer';
import type { Resume, ResumeContent } from '@/types/resume';
import { findEvidenceFocusRecord } from '@/lib/pilotEvidenceFocus';
import styles from './ResumeLibraryView.module.css';

const BLANK_RESUME_CONTENT: ResumeContent = {
  career_intent: { target_roles: [], target_locations: [] },
  contact: {},
  education: [],
  experience: [],
  projects: [],
  skills: [],
  raw_text: '',
};

interface ResumeLibraryViewProps {
  /** Increases when the shell asks this page to open its existing upload flow. */
  uploadRequestToken?: number;
  onUploadRequestConsumed?: () => void;
  onAttachToPilot?: (attachment: import('@/types/chat').PilotContextAttachment) => void;
  focusResumeId?: number;
  onEvidenceFocusConsumed?: () => void;
  onboardingFocusToken?: number;
}

export default function ResumeLibraryView({
  uploadRequestToken,
  onUploadRequestConsumed,
  onAttachToPilot,
  focusResumeId,
  onEvidenceFocusConsumed,
  onboardingFocusToken,
}: ResumeLibraryViewProps) {
  const qc = useQueryClient();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [editing, setEditing] = useState<Resume | null>(null);
  const [compareTargetId, setCompareTargetId] = useState<number | null>(null);
  const [keyword, setKeyword] = useState('');
  const [dragActive, setDragActive] = useState(false);
  const dragCounter = useRef(0);
  const lastUploadRequestTokenRef = useRef<number | undefined>(
    onUploadRequestConsumed ? 0 : uploadRequestToken,
  );
  const onboardingEntryRef = useRef<HTMLDivElement>(null);
  const [onboardingFocusActive, setOnboardingFocusActive] = useState(false);

  const resumesQuery = useQuery({ queryKey: ['resumes'], queryFn: listResumes });

  const createDialogMut = useMutation({
    mutationFn: () =>
      createResume({
        title: 'Haru 对话初稿简历',
        source: 'dialog',
        content_json: BLANK_RESUME_CONTENT,
        career_intent: BLANK_RESUME_CONTENT.career_intent,
      }),
    onSuccess: (res) => {
      message.success('已创建初稿简历');
      qc.invalidateQueries({ queryKey: ['resumes'] });
      qc.invalidateQueries({ queryKey: ONBOARDING_QUERY_KEY });
      setEditing(res);
    },
    onError: () => message.error('创建失败'),
  });

  const sampleMut = useMutation({
    mutationFn: () => createResumeFromSample({ sample_id: 'backend' }),
    onSuccess: (res) => {
      message.success('已从样例创建');
      qc.invalidateQueries({ queryKey: ['resumes'] });
      qc.invalidateQueries({ queryKey: ONBOARDING_QUERY_KEY });
      setEditing(res);
    },
    onError: () => message.error('创建样例失败'),
  });

  const uploadMut = useMutation({
    mutationFn: (file: File) => uploadResume(file),
    onSuccess: (res) => {
      message.success(res.parse_status === 'text-ready' ? '已提取文字，可在简历编辑器中进行 AI 分类并核对' : '已上传，但未提取到文字；请检查文件是否为扫描件');
      qc.invalidateQueries({ queryKey: ['resumes'] });
      qc.invalidateQueries({ queryKey: ONBOARDING_QUERY_KEY });
      setUploadOpen(false);
      setEditing(res);
    },
    onError: () => message.error('上传失败'),
  });

  const setMasterMut = useMutation({
    mutationFn: (id: number) => updateResume(id, { is_master: true }),
    onSuccess: (res) => {
      message.success('已设为基础简历');
      qc.invalidateQueries({ queryKey: ['resumes'] });
      qc.invalidateQueries({ queryKey: ONBOARDING_QUERY_KEY });
      setEditing(res);
    },
    onError: () => message.error('设置基础简历失败'),
  });

  const copyMut = useMutation({
    mutationFn: (id: number) => copyResume(id),
    onSuccess: (res) => {
      message.success('已复制简历');
      qc.invalidateQueries({ queryKey: ['resumes'] });
      setEditing(res);
    },
    onError: () => message.error('复制失败'),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteResume(id),
    onSuccess: () => {
      message.success('已删除');
      qc.invalidateQueries({ queryKey: ['resumes'] });
      qc.invalidateQueries({ queryKey: ONBOARDING_QUERY_KEY });
    },
    onError: (error: any) => {
      const detail = error?.response?.data?.error;
      message.error(detail === 'master resume cannot be deleted' ? '基础简历不可删除' : '删除失败');
    },
  });

  const uploadFile = (file: File) => {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      message.error('仅支持 PDF 简历');
      return;
    }
    uploadMut.mutate(file);
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    dragCounter.current = 0;
    setDragActive(false);
    const file = e.dataTransfer.files?.[0];
    if (file) uploadFile(file);
  };

  const resumes = resumesQuery.data ?? [];
  const compareTarget = compareTargetId === null
    ? undefined
    : resumes.find((resume) => resume.id === compareTargetId);

  useEffect(() => {
    if (uploadRequestToken === undefined) return;
    const previous = lastUploadRequestTokenRef.current;
    lastUploadRequestTokenRef.current = uploadRequestToken;
    if (previous !== undefined && uploadRequestToken > previous) {
      setUploadOpen(true);
      onUploadRequestConsumed?.();
    }
  }, [onUploadRequestConsumed, uploadRequestToken]);

  useEffect(() => {
    if (compareTargetId !== null && !resumes.some((resume) => resume.id === compareTargetId)) {
      setCompareTargetId(null);
    }
  }, [compareTargetId, resumes]);

  useEffect(() => {
    if (
      focusResumeId === undefined ||
      resumesQuery.isLoading ||
      resumesQuery.isError ||
      resumesQuery.isFetching
    ) return;
    const resume = findEvidenceFocusRecord(resumes, focusResumeId);
    if (resume) {
      setEditing(resume);
    } else {
      message.warning('引用的记录已不存在');
    }
    onEvidenceFocusConsumed?.();
  }, [
    focusResumeId,
    resumes,
    resumesQuery.isLoading,
    resumesQuery.isError,
    resumesQuery.isFetching,
    onEvidenceFocusConsumed,
  ]);

  useEffect(() => {
    if (editing) {
      window.scrollTo({ top: 0, left: 0 });
    }
  }, [editing]);

  useEffect(() => {
    if (!onboardingFocusToken || resumesQuery.isLoading) return;

    setOnboardingFocusActive(true);
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    onboardingEntryRef.current?.scrollIntoView({
      behavior: reducedMotion ? 'auto' : 'smooth',
      block: 'center',
      inline: 'nearest',
    });
    onboardingEntryRef.current?.focus({ preventScroll: true });
    const timeout = window.setTimeout(() => setOnboardingFocusActive(false), 2400);
    return () => window.clearTimeout(timeout);
  }, [onboardingFocusToken, resumesQuery.isLoading]);

  if (resumesQuery.isLoading) {
    return (
      <div role="status" style={{ textAlign: 'center', padding: 48 }}>
        <Spin />
        <div>正在加载简历</div>
      </div>
    );
  }

  if (resumesQuery.isError) {
    return (
      <div role="alert" style={{ textAlign: 'center', padding: 48 }}>
        <div style={{ marginBottom: 12 }}>加载简历失败</div>
        <Button onClick={() => void resumesQuery.refetch()}>重试</Button>
      </div>
    );
  }

  const kw = keyword.trim().toLowerCase();
  const filtered = resumes.filter((r) => {
    if (!kw) return true;
    return [
      r.title,
      r.name,
      r.source,
      ...(r.missing_sections ?? []),
    ].join(' ').toLowerCase().includes(kw);
  });

  if (editing) {
    return (
      <ResumeEditorDrawer
        resume={editing}
        resumes={resumes}
        open={!!editing}
        onClose={() => setEditing(null)}
        onSaved={(next) => setEditing(next)}
        onFactVersionCreated={(created) => {
          qc.setQueryData<Resume[]>(['resumes'], (current = []) => [
            created,
            ...current.filter((candidate) => candidate.id !== created.id),
          ]);
          setEditing(null);
          setCompareTargetId(created.id);
        }}
        onFactCopyCreated={(created) => {
          qc.setQueryData<Resume[]>(['resumes'], (current = []) => [
            created,
            ...current.filter((candidate) => candidate.id !== created.id),
          ]);
        }}
        onFactContinueInCopy={(created) => {
          qc.setQueryData<Resume[]>(['resumes'], (current = []) => [
            created,
            ...current.filter((candidate) => candidate.id !== created.id),
          ]);
          setEditing(created);
        }}
        onFactExitToLibrary={() => {
          setEditing(null);
          void qc.invalidateQueries({ queryKey: ['resumes'] });
        }}
        onFactCopyResultUnknown={() => {
          void qc.invalidateQueries({ queryKey: ['resumes'] });
        }}
      />
    );
  }

  return (
    <div
      onDragEnter={(e) => { e.preventDefault(); dragCounter.current++; setDragActive(true); }}
      onDragOver={(e) => e.preventDefault()}
      onDragLeave={() => { dragCounter.current--; if (dragCounter.current <= 0) { setDragActive(false); dragCounter.current = 0; } }}
      onDrop={handleDrop}
    >
      <div className={styles.header}>
        <div>
          <div className={styles.title}>简历库</div>
          <div className={styles.subtitle}>共 {filtered.length} 份 · 拖入 PDF 至任意位置可上传</div>
        </div>
        <div
          ref={onboardingEntryRef}
          className={`${styles.headerActions} ${onboardingFocusActive ? styles.onboardingFocus : ''}`}
          tabIndex={-1}
          data-onboarding-target="resume-create"
          aria-label="创建基础简历入口"
        >
          <Input.Search
            placeholder="搜索简历"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            allowClear
            style={{ width: 200 }}
          />
          <Button
            icon={<PlusOutlined />}
            loading={createDialogMut.isPending}
            onClick={() => createDialogMut.mutate()}
          >
             和 Haru 创建初稿
           </Button>
           <Button icon={<CloudUploadOutlined />} onClick={() => setUploadOpen(true)}>上传现有简历</Button>
           <Dropdown menu={{ items: [{ key: 'sample', label: '用样例开始', icon: <FileAddOutlined />, onClick: () => sampleMut.mutate() }] }}>
             <Button icon={<MoreOutlined />} loading={sampleMut.isPending}>更多创建方式</Button>
           </Dropdown>
        </div>
      </div>

      {resumes.length === 0 ? (
        <div className={styles.emptyState}>
          <div className={styles.emptyIcon}><FileTextOutlined /></div>
          <div className={styles.emptyTitle}>还没有简历</div>
          <div className={styles.emptyHint}>选择一个入口开始，之后都可以在编辑器里补全结构化章节。</div>
          <div className={styles.emptyActions}>
            <button className={styles.emptyAction} type="button" onClick={() => createDialogMut.mutate()}>
               <div className={styles.emptyActionTitle}>和 Haru 创建初稿</div>
              <div className={styles.emptyActionDesc}>先生成可编辑的空结构，再逐章补充。</div>
            </button>
            <button className={styles.emptyAction} type="button" onClick={() => setUploadOpen(true)}>
               <div className={styles.emptyActionTitle}>上传现有简历</div>
              <div className={styles.emptyActionDesc}>继续使用现有上传流程，仅支持 PDF。</div>
            </button>
          </div>
        </div>
      ) : filtered.length === 0 ? (
        <div className={styles.dropZone}>
          <div className={styles.dropTitle}>没有匹配的简历</div>
          <div className={styles.dropHint}>换个关键词，或从右上角创建新简历。</div>
        </div>
      ) : (
        <div className={styles.grid}>
          {filtered.map((r, i) => (
            <div key={r.id} className={styles.card} style={{ animationDelay: `${Math.min(i, 6) * 60}ms` }}>
              <ResumeCard
                resume={r}
                resumes={resumes}
                onEdit={() => setEditing(r)}
                onSetMaster={() => setMasterMut.mutate(r.id)}
                onCopy={() => copyMut.mutate(r.id)}
                onDelete={() => deleteMut.mutate(r.id)}
                onCompare={resumes.length > 1 ? () => setCompareTargetId(r.id) : undefined}
                onAttachToPilot={onAttachToPilot}
              />
            </div>
          ))}
        </div>
      )}

      {resumes.length > 0 && !resumesQuery.isLoading && (
        <div className={`${styles.dropZone} ${styles.dropZoneCompact} ${dragActive ? styles.dropZoneActive : ''}`} style={{ marginTop: 16 }}>
          <div className={styles.dropTitle}>拖拽 PDF 到此处上传</div>
           <div className={styles.dropHint}>或点击「上传现有简历」按钮</div>
        </div>
      )}

      {dragActive && <div className={styles.overlay}>松开以上传 PDF 简历</div>}

      <ResumeUploadModal
        open={uploadOpen}
        uploading={uploadMut.isPending}
        onSubmit={uploadFile}
        onClose={() => setUploadOpen(false)}
      />

      {compareTarget && (
        <ResumeVersionCompareDrawer
          open
          target={compareTarget}
          candidates={resumes}
          onClose={() => setCompareTargetId(null)}
        />
      )}
    </div>
  );
}
