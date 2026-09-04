import { useEffect, type Ref } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeftOutlined } from '@ant-design/icons';
import { Button, DatePicker, Form, Input, InputNumber, Select, Space, message } from 'antd';
import dayjs from 'dayjs';
import type { Dayjs } from 'dayjs';
import { createEvent, updateEvent } from '@/services/events';
import type { Application } from '@/types/application';
import type { ScheduleEvent, ScheduleEventInput, ScheduleEventType } from '@/types/event';
import { EVENT_TYPE_LABELS } from '@/types/event';
import workflowStyles from './ui/WorkflowSurface.module.css';

interface ScheduleEventFormProps {
  open: boolean;
  applications: Application[];
  initialApplication?: Application;
  event?: ScheduleEvent;
  onClose: () => void;
  headingRef?: Ref<HTMLHeadingElement>;
  onSuccess?: (event: ScheduleEvent) => void;
}

interface ScheduleEventFormValues {
  application_id: number;
  event_type: ScheduleEventType;
  subtype?: string;
  tags?: string[];
  round?: number | null;
  scheduled_at: Dayjs;
  remind_at?: Dayjs | null;
  duration_minutes: number;
  location?: string;
  notes?: string;
  status?: string;
}

const EVENT_TYPE_OPTIONS = (Object.entries(EVENT_TYPE_LABELS) as [ScheduleEventType, string][]).map(
  ([value, label]) => ({ value, label })
);

function getDefaultScheduledAt() {
  return dayjs().add(1, 'day').startOf('hour');
}

export default function ScheduleEventForm({
  open,
  applications,
  initialApplication,
  event,
  onClose,
  headingRef,
  onSuccess,
}: ScheduleEventFormProps) {
  const [form] = Form.useForm<ScheduleEventFormValues>();
  const queryClient = useQueryClient();
  const isEdit = !!event;

  const mutation = useMutation({
    mutationFn: (input: ScheduleEventInput) =>
      isEdit ? updateEvent(event.id, input) : createEvent(input),
    onSuccess: (savedEvent) => {
      message.success(isEdit ? '日程已更新' : '日程已创建');
      onSuccess?.(savedEvent);
      queryClient.invalidateQueries({ queryKey: ['calendar'] });
      queryClient.invalidateQueries({ queryKey: ['events'] });
      form.resetFields();
      onClose();
    },
    onError: () => {
      message.error(isEdit ? '更新日程失败' : '创建日程失败');
    },
  });

  useEffect(() => {
    if (!open) return;

    if (event) {
      form.setFieldsValue({
        application_id: event.application_id,
        event_type: event.event_type,
        subtype: event.subtype,
        tags: event.tags,
        round: event.round,
        scheduled_at: dayjs(event.scheduled_at),
        remind_at: event.remind_at ? dayjs(event.remind_at) : null,
        duration_minutes: event.duration_minutes,
        location: event.location,
        notes: event.notes,
        status: event.status,
      });
      return;
    }

    form.setFieldsValue({
      application_id: initialApplication?.id,
      event_type: 'interview',
      subtype: '',
      tags: [],
      round: 0,
      scheduled_at: getDefaultScheduledAt(),
      remind_at: null,
      duration_minutes: 60,
      location: '',
      notes: '',
      status: 'todo',
    });
  }, [event, form, initialApplication, open]);

  const handleClose = () => {
    form.resetFields();
    onClose();
  };

  const handleFinish = (values: ScheduleEventFormValues) => {
    mutation.mutate({
      application_id: values.application_id,
      event_type: values.event_type,
      subtype: values.subtype ?? '',
      tags: values.tags ?? [],
      round: values.round ?? 0,
      scheduled_at: values.scheduled_at.toISOString(),
      remind_at: values.remind_at ? values.remind_at.toISOString() : null,
      duration_minutes: values.duration_minutes,
      location: values.location ?? '',
      notes: values.notes ?? '',
      status: values.status ?? 'todo',
    });
  };

  if (!open) return null;

  return (
    <section data-testid="schedule-event-form" aria-label={isEdit ? '编辑日程' : '新建日程'} className={`${workflowStyles.surface} ${workflowStyles.stack}`}>
      <div className={workflowStyles.sectionHeader}>
        <div>
          <Button
            type="link"
            icon={<ArrowLeftOutlined />}
            onClick={handleClose}
            style={{ width: 'fit-content', height: 'auto', padding: 0 }}
          >
            返回上一层
          </Button>
          <h2 ref={headingRef} tabIndex={-1} style={{ margin: '8px 0 0' }}>
            {isEdit ? '编辑日程' : '新建日程'}
          </h2>
        </div>
        <Space className={workflowStyles.actionGroup}>
          <Button onClick={handleClose}>取消</Button>
          <Button type="primary" loading={mutation.isPending} onClick={() => form.submit()}>
            {isEdit ? '保存' : '创建'}
          </Button>
        </Space>
      </div>
      <Form form={form} layout="vertical" onFinish={handleFinish} requiredMark={false} className={workflowStyles.section}>
        <Form.Item
          name="application_id"
          label="投递"
          rules={[{ required: true, message: '请选择投递' }]}
        >
          <Select
            showSearch
            disabled={!!initialApplication || isEdit}
            placeholder="选择投递"
            optionFilterProp="label"
            options={applications.map((application) => ({
              value: application.id,
              label: `${application.company_name} · ${application.position_name}`,
            }))}
          />
        </Form.Item>

        <div className={workflowStyles.formGridCompact}>
          <Form.Item
            name="event_type"
            label="类型"
            rules={[{ required: true, message: '请选择类型' }]}
          >
            <Select options={EVENT_TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item name="round" label="轮次">
            <InputNumber min={0} precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </div>

        <Form.Item name="subtype" label="子类型">
          <Input placeholder="例如 assessment / technical / negotiation" />
        </Form.Item>

        <Form.Item name="tags" label="标签">
          <Select mode="tags" placeholder="输入后回车添加标签" tokenSeparators={[',', '，']} />
        </Form.Item>

        <div className={workflowStyles.formGridCompact}>
          <Form.Item
            name="scheduled_at"
            label="时间"
            rules={[{ required: true, message: '请选择时间' }]}
          >
            <DatePicker showTime style={{ width: '100%' }} format="YYYY-MM-DD HH:mm" />
          </Form.Item>
          <Form.Item
            name="duration_minutes"
            label="时长"
            rules={[{ required: true, message: '请输入时长' }]}
          >
            <InputNumber min={1} precision={0} addonAfter="分钟" style={{ width: '100%' }} />
          </Form.Item>
        </div>

        <div className={workflowStyles.formGridStatus}>
          <Form.Item name="remind_at" label="提醒时间">
            <DatePicker showTime style={{ width: '100%' }} format="YYYY-MM-DD HH:mm" />
          </Form.Item>
          <Form.Item name="status" label="状态">
            <Select
              options={[
                { value: 'todo', label: '待处理' },
                { value: 'done', label: '已完成' },
                { value: 'cancelled', label: '已取消' },
              ]}
            />
          </Form.Item>
        </div>

        <Form.Item name="location" label="地点">
          <Input placeholder="线上链接或线下地址" />
        </Form.Item>

        <Form.Item name="notes" label="备注">
          <Input.TextArea rows={3} placeholder="准备事项、联系人等" />
        </Form.Item>
      </Form>
    </section>
  );
}
