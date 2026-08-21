import { useMemo, useState } from 'react';
import {
  DragOverlay,
  type DragEndEvent,
  type DragStartEvent,
  useDndMonitor,
} from '@dnd-kit/core';
import { useQueryClient } from '@tanstack/react-query';
import { Input, Modal, Typography, message } from 'antd';
import { updateApplication } from '@/services/applications';
import {
  KANBAN_COLUMNS,
  STATUS_COLORS,
  STATUS_LABELS,
} from '@/types/application';
import type { Application, ApplicationStatus } from '@/types/application';
import KanbanColumn from './KanbanColumn';
import KanbanCard from './KanbanCard';
import {
  buildApplicationStatusPayload,
  requiresClosedReason,
  resolveKanbanDropDestination,
  willRecordFirstStatusTimestamp,
  filterAndSortApplications,
  DEFAULT_APPLICATION_VIEW_STATE,
  type ApplicationViewState,
} from './applicationLifecycle';
import styles from './KanbanBoard.module.css';

interface KanbanBoardProps {
  applications: Application[];
  onOpenDetail?: (app: Application) => void;
  onAttachToPilot?: (attachment: import('@/types/chat').PilotContextAttachment) => void;
  viewState?: ApplicationViewState;
}

export default function KanbanBoard({ applications, onOpenDetail, onAttachToPilot, viewState }: KanbanBoardProps) {
  const queryClient = useQueryClient();
  const [activeId, setActiveId] = useState<number | null>(null);
  const [pendingMove, setPendingMove] = useState<{ app: Application; status: ApplicationStatus } | null>(null);
  const [closedReason, setClosedReason] = useState('');
  const [saving, setSaving] = useState(false);

  const columns = useMemo(() => {
    const grouped = {} as Record<ApplicationStatus, Application[]>;
    for (const s of KANBAN_COLUMNS) grouped[s] = [];
    filterAndSortApplications(applications, viewState ?? DEFAULT_APPLICATION_VIEW_STATE).forEach((app) => {
      if (grouped[app.status]) grouped[app.status].push(app);
    });
    return grouped;
  }, [applications, viewState]);

  const activeRecord = applications.find((a) => a.id === activeId) ?? null;

  const handleDragStart = (event: DragStartEvent) => {
    setActiveId(event.active.id as number);
  };

  const requestStatusChange = (app: Application, newStatus: ApplicationStatus) => {
    if (newStatus === app.status) return;
    setPendingMove({ app, status: newStatus });
    setClosedReason('');
  };

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    setActiveId(null);
    if (!over) return;

    const appId = active.id as number;
    const app = applications.find((item) => item.id === appId);
    if (!app) return;

    const destination = resolveKanbanDropDestination(String(over.id));
    if (!destination) return;
    if (destination.kind === 'pilot') {
      onAttachToPilot?.({
        kind: 'application',
        id: String(app.id),
        label: `${app.company_name} · ${app.position_name}`,
      });
      return;
    }
    if (app.status !== destination.status) requestStatusChange(app, destination.status);
  };

  const confirmStatusChange = async () => {
    if (!pendingMove) return;
    if (requiresClosedReason(pendingMove.app.status, pendingMove.status) && !closedReason.trim()) {
      message.error('请填写关闭原因');
      return;
    }
    setSaving(true);
    try {
      await updateApplication(
        pendingMove.app.id,
        buildApplicationStatusPayload(pendingMove.app, pendingMove.status, closedReason)
      );
      queryClient.invalidateQueries({ queryKey: ['applications'] });
      queryClient.invalidateQueries({ queryKey: ['events'] });
      message.success(`已移至「${STATUS_LABELS[pendingMove.status]}」`);
      setPendingMove(null);
      setClosedReason('');
    } catch {
      message.error('状态更新失败，请重试');
    } finally {
      setSaving(false);
    }
  };

  const handleDragCancel = () => {
    setActiveId(null);
  };

  useDndMonitor({
    onDragStart: handleDragStart,
    onDragEnd: handleDragEnd,
    onDragCancel: handleDragCancel,
  });

  return (
    <>
      <div className={styles.board}>
        {KANBAN_COLUMNS.map((status) => (
          <KanbanColumn
            key={status}
            status={status}
            label={STATUS_LABELS[status]}
            color={STATUS_COLORS[status]}
            cards={columns[status]}
            activeId={activeId}
            onOpenDetail={onOpenDetail}
            onRequestStatusChange={requestStatusChange}
          />
        ))}
      </div>
      <DragOverlay dropAnimation={null}>
        {activeRecord ? <KanbanCard record={activeRecord} overlay /> : null}
      </DragOverlay>
      <Modal
        title="确认更新投递状态"
        open={!!pendingMove}
        okText="确认更新"
        cancelText="取消"
        confirmLoading={saving}
        onOk={confirmStatusChange}
        onCancel={() => {
          setPendingMove(null);
          setClosedReason('');
        }}
      >
        {pendingMove && (
          <div className={styles.confirmBody}>
            <Typography.Paragraph>
              {pendingMove.app.company_name} · {pendingMove.app.position_name}
            </Typography.Paragraph>
            <Typography.Paragraph type="secondary">
              从「{STATUS_LABELS[pendingMove.app.status]}」移动到「{STATUS_LABELS[pendingMove.status]}」。
              {willRecordFirstStatusTimestamp(pendingMove.app, pendingMove.status)
                ? '首次进入该状态，将记录对应时间。'
                : '该状态已有首次时间记录，本次不会覆盖。'}
            </Typography.Paragraph>
            {requiresClosedReason(pendingMove.app.status, pendingMove.status) && (
              <Input.TextArea
                rows={3}
                value={closedReason}
                onChange={(event) => setClosedReason(event.target.value)}
                placeholder="填写关闭原因，例如：岗位关闭、主动放弃、已接受其他 offer"
              />
            )}
          </div>
        )}
      </Modal>
    </>
  );
}
