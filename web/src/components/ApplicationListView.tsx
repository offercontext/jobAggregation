import { Button, Input, Select, Table, Tag } from 'antd';
import { RobotOutlined } from '@ant-design/icons';
import { useMemo, useState } from 'react';
import dayjs from 'dayjs';
import type { ColumnsType } from 'antd/es/table';
import type { Application, ApplicationStatus } from '@/types/application';
import { KANBAN_COLUMNS, STATUS_LABELS, STATUS_COLORS } from '@/types/application';
import type { ScheduleEvent } from '@/types/event';
import {
  filterAndSortApplications,
  formatNextApplicationEvent,
  DEFAULT_APPLICATION_VIEW_STATE,
  type ApplicationSortBy,
  type ApplicationViewState,
} from './KanbanBoard/applicationLifecycle';
import styles from './ApplicationListView.module.css';
import { createPilotAttachmentDragBinding } from './PilotAttachmentHandle';

interface ApplicationListViewProps {
  applications: Application[];
  events: ScheduleEvent[];
  onOpenDetail: (app: Application) => void;
  onAskPilot: (app: Application) => void;
  onAttachToPilot?: (attachment: import('@/types/chat').PilotContextAttachment) => void;
  viewState?: ApplicationViewState;
  onViewStateChange?: (state: ApplicationViewState) => void;
}

const STATUS_FILTERS = [
  { value: 'all', label: '全部状态' },
  ...KANBAN_COLUMNS.map((status) => ({ value: status, label: STATUS_LABELS[status] })),
];

const SORT_OPTIONS: { value: ApplicationSortBy; label: string }[] = [
  { value: 'updated_desc', label: '最近更新优先' },
  { value: 'updated_asc', label: '最早更新优先' },
  { value: 'applied_desc', label: '最近投递优先' },
  { value: 'applied_asc', label: '最早投递优先' },
];

export default function ApplicationListView({
  applications,
  events,
  onOpenDetail,
  onAskPilot,
  onAttachToPilot,
  viewState,
  onViewStateChange,
}: ApplicationListViewProps) {
  const [localViewState, setLocalViewState] = useState<ApplicationViewState>(DEFAULT_APPLICATION_VIEW_STATE);
  const currentViewState = viewState ?? localViewState;
  const { keyword, status, sortBy } = currentViewState;
  const updateViewState = (patch: Partial<ApplicationViewState>) => {
    const next = { ...currentViewState, ...patch };
    if (onViewStateChange) onViewStateChange(next);
    else setLocalViewState(next);
  };

  const rows = useMemo(
    () => filterAndSortApplications(applications, { keyword, status, sortBy }),
    [applications, keyword, status, sortBy]
  );

  const columns: ColumnsType<Application> = [
    {
      title: '投递',
      key: 'application',
      render: (_, row) => (
        <div className={styles.meta}>
          <span className={styles.company}>{row.company_name}</span>
          <span className={styles.position}>{row.position_name}</span>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 120,
      render: (value: ApplicationStatus) => (
        <Tag color={STATUS_COLORS[value]}>{STATUS_LABELS[value]}</Tag>
      ),
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 120,
      render: (value: string) => <span className={styles.muted}>{value || '-'}</span>,
    },
    {
      title: '下一事件',
      key: 'next_event',
      width: 220,
      render: (_, row) => <span className={styles.muted}>{formatNextApplicationEvent(row, events)}</span>,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 150,
      render: (value: string) => <span className={styles.muted}>{dayjs(value).format('YYYY-MM-DD HH:mm')}</span>,
    },
    {
      title: 'Pilot',
      key: 'pilot',
      width: 112,
      render: (_, row) => (
        <div onClick={(event) => event.stopPropagation()}>
          <Button
            type="link"
            size="small"
            icon={<RobotOutlined />}
            onClick={() => onAskPilot(row)}
          >
            问 Pilot
          </Button>
        </div>
      ),
    },
  ];

  return (
    <section aria-label="投递列表">
      <div className={styles.toolbar}>
        <Input.Search
          className={styles.search}
          allowClear
          placeholder="搜索公司、岗位、备注"
          value={keyword}
          onChange={(event) => updateViewState({ keyword: event.target.value })}
        />
        <Select
          aria-label="状态"
          value={status}
          options={STATUS_FILTERS}
          onChange={(value) => updateViewState({ status: value })}
          style={{ width: 140 }}
        />
        <Select
          aria-label="排序"
          value={sortBy}
          options={SORT_OPTIONS}
          onChange={(value) => updateViewState({ sortBy: value })}
          style={{ width: 160 }}
        />
      </div>
      <Table<Application>
        rowKey="id"
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        onRow={(row) => ({
          ...(onAttachToPilot
            ? createPilotAttachmentDragBinding({
                kind: 'application',
                id: String(row.id),
                label: `${row.company_name} · ${row.position_name}`,
              })
            : {}),
          onClick: () => onOpenDetail(row),
          style: { cursor: 'pointer' },
        })}
      />
    </section>
  );
}
