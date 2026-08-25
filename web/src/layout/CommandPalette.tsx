import { useEffect, useMemo, useState } from 'react';
import { Modal, Input, List } from 'antd';
import type { Application } from '@/types/application';
import type { PipelineInsight } from '@/lib/pipelineInsights';
import workflowStyles from '@/components/ui/WorkflowSurface.module.css';
import { MODULE_NAV, type ViewMode } from './navigation';

export interface Command {
  key: string;
  label: string;
  hint?: string;
  run: () => void;
}

function commandOptionId(key: string): string {
  return `command-palette-option-${key.replace(/[^A-Za-z0-9_-]/g, '-')}`;
}

function pipelineInsightMatches(item: PipelineInsight, keyword: string): boolean {
  if (!keyword) return true;

  const hint = `流程提醒 - ${item.priority.toUpperCase()}`;
  return [
    item.title,
    hint,
    item.kind,
    item.reason,
    item.primaryAction.label,
    ...item.evidence,
  ]
    .join(' ')
    .toLowerCase()
    .includes(keyword);
}

export function buildApplicationSearchCommands(
  applications: Application[],
  keyword: string,
  onOpenDetail: (app: Application) => void,
  onClose: () => void
): Command[] {
  const kw = keyword.trim().toLowerCase();
  if (!kw) return [];

  return applications
    .filter((a) => !a.deleted_at)
    .filter(
      (a) =>
        a.company_name.toLowerCase().includes(kw) ||
        a.position_name.toLowerCase().includes(kw)
    )
    .slice(0, 6)
    .map((a) => ({
      key: `app-${a.id}`,
      label: `${a.company_name} · ${a.position_name}`,
      hint: '投递',
      run: () => {
        onOpenDetail(a);
        onClose();
      },
    }));
}

export function buildPipelineNavigationCommands(
  onNavigate: (v: ViewMode) => void,
  onClose: () => void
): Command[] {
  return [
    { key: 'pipeline-board', label: '打开投递看板', hint: '投递', view: 'board' },
    { key: 'pipeline-list', label: '打开投递列表', hint: '投递', view: 'applications-list' },
    { key: 'pipeline-calendar', label: '打开事件日历', hint: '今日', view: 'calendar' },
    { key: 'pipeline-reminders', label: '打开今日提醒', hint: '今日', view: 'reminders' },
    { key: 'pipeline-offers', label: '打开 Offer 中心', hint: 'Offer', view: 'offers' },
  ].map((item) => ({
    key: item.key,
    label: item.label,
    hint: item.hint,
    run: () => {
      onNavigate(item.view as ViewMode);
      onClose();
    },
  }));
}

interface Props {
  open: boolean;
  onClose: () => void;
  applications: Application[];
  onNavigate: (v: ViewMode) => void;
  onOpenDetail: (app: Application) => void;
  onAddApplication: () => void;
  onOpenResume: () => void;
  onUploadResume?: () => void;
  onOpenChat: () => void;
  onOpenPilot?: () => void;
  onOpenSettings: () => void;
  pipelineActions: PipelineInsight[];
  onRunPipelineAction: (item: PipelineInsight) => void;
}

export default function CommandPalette({
  open,
  onClose,
  applications,
  onNavigate,
  onOpenDetail,
  onAddApplication,
  onOpenResume,
  onUploadResume,
  onOpenChat,
  onOpenPilot,
  onOpenSettings,
  pipelineActions,
  onRunPipelineAction,
}: Props) {
  const [q, setQ] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);

  useEffect(() => {
    if (!open) setQ('');
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [open, q]);

  const actions: Command[] = useMemo(
    () => [
      { key: 'add', label: '添加投递', hint: '动作', run: () => { onAddApplication(); onClose(); } },
      { key: 'resume-library', label: '打开简历库', hint: '简历', run: () => { onOpenResume(); onClose(); } },
      { key: 'new-resume', label: '新建简历', hint: '在简历库创建薄版', run: () => { onOpenResume(); onClose(); } },
      { key: 'uploadResume', label: '上传简历', hint: 'PDF 到简历库', run: () => { onUploadResume?.(); onClose(); } },
      { key: 'haru', label: '问 Haru', hint: '助手', run: () => { onOpenChat(); onClose(); } },
      { key: 'pilot', label: '打开 Pilot 工作区', hint: '助手', run: () => { onOpenPilot?.(); onClose(); } },
      { key: 'settings', label: '打开设置', hint: '设置', run: () => { onOpenSettings(); onClose(); } },
      ...buildPipelineNavigationCommands(onNavigate, onClose),
      ...MODULE_NAV.map((item) => ({
        key: `nav-${item.key}`,
        label: `前往 ${item.label}`,
        hint: '导航',
        run: () => {
          onNavigate(item.defaultView);
          onClose();
        },
      })),
    ],
    [onAddApplication, onOpenResume, onUploadResume, onOpenChat, onOpenPilot, onOpenSettings, onNavigate, onClose]
  );

  const kw = q.trim().toLowerCase();
  const pipelineCommands: Command[] = useMemo(
    () =>
      pipelineActions
        .filter((item) => pipelineInsightMatches(item, kw))
        .slice(0, 5)
        .map((item) => ({
          key: `pipeline-${item.id}`,
          label: item.title,
          hint: `流程提醒 - ${item.priority.toUpperCase()}`,
          run: () => {
            onRunPipelineAction(item);
            onClose();
          },
        })),
    [pipelineActions, kw, onRunPipelineAction, onClose]
  );
  const appMatches = buildApplicationSearchCommands(applications, kw, onOpenDetail, onClose);

  const actionMatches = kw
    ? actions.filter((c) => c.label.toLowerCase().includes(kw))
    : actions;

  const items = [...appMatches, ...pipelineCommands, ...actionMatches];
  const activeItem = items[activeIndex];

  useEffect(() => {
    if (!open || !activeItem) return;
    document.getElementById(commandOptionId(activeItem.key))?.scrollIntoView({ block: 'nearest' });
  }, [open, activeItem?.key]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, items.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      items[activeIndex]?.run();
    }
  };

  return (
    <Modal open={open} onCancel={onClose} footer={null} closable={false} width={520} styles={{ body: { padding: 0 } }}>
      <Input
        autoFocus
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls="command-palette-listbox"
        aria-activedescendant={activeItem ? commandOptionId(activeItem.key) : undefined}
        size="large"
        variant="borderless"
        placeholder="快速打开页面、投递或助手…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={onKeyDown}
        style={{ padding: '14px 16px' }}
      />
      <div
        id="command-palette-listbox"
        role="listbox"
        aria-label="命令结果"
        data-testid="command-palette-results"
        className={workflowStyles.scrollRegion}
        style={{ maxHeight: 360, borderTop: '1px solid var(--op-border)' }}
      >
        <List
          dataSource={items}
          locale={{ emptyText: '无匹配结果' }}
          renderItem={(c, index) => (
            <List.Item
              id={commandOptionId(c.key)}
              role="option"
              aria-selected={index === activeIndex}
              tabIndex={-1}
              className={workflowStyles.listRow}
              onClick={c.run}
              onMouseEnter={() => setActiveIndex(index)}
              style={{
                minHeight: 40,
                padding: '10px 16px',
                cursor: 'pointer',
                background: index === activeIndex ? 'var(--op-layout-bg)' : undefined,
              }}
            >
              <span className="op-long-text" style={{ color: 'var(--op-ink)' }}>{c.label}</span>
              {c.hint && <span style={{ fontSize: 11, color: 'var(--op-muted)' }}>{c.hint}</span>}
            </List.Item>
          )}
        />
      </div>
    </Modal>
  );
}
