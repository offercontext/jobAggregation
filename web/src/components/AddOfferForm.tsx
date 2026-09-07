import { useEffect, useRef } from 'react';
import { Alert, Button, Modal, Form, Input, InputNumber, Select, App as AntApp } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { Application } from '@/types/application';
import type { Offer, OfferInput, OfferStatus } from '@/types/offer';
import { OFFER_STATUS_LABELS } from '@/types/offer';
import { createOffer, updateOffer } from '@/services/offers';
import { listOfferBindingState } from './offerWorkspaceModel';

interface Props {
  open: boolean;
  onClose: () => void;
  applications: Application[];
  editing?: Offer | null;
  requestToken?: string | null;
}

const STATUS_OPTIONS = (Object.keys(OFFER_STATUS_LABELS) as OfferStatus[]).map((s) => ({
  value: s,
  label: OFFER_STATUS_LABELS[s],
}));

export default function AddOfferForm({ open, onClose, applications, editing, requestToken }: Props) {
  const [form] = Form.useForm();
  const { message: toast } = AntApp.useApp();
  const qc = useQueryClient();
  const historicalReadOnly = Boolean(editing && listOfferBindingState(editing) === 'unbound');
  const autoFilledApplicationFields = useRef<{
    company_name?: string;
    position_name?: string;
  }>({});

  useEffect(() => {
    if (open) {
      autoFilledApplicationFields.current = {};
      if (editing) {
        form.setFieldsValue({ ...editing, application_id: editing.application_id });
      } else {
        form.resetFields();
        form.setFieldsValue({ months_per_year: 12, status: 'pending' });
      }
    }
  }, [open, editing, form]);

  const applicationOptions = applications.map((application) => ({
    value: application.id,
    label: `#${application.id} ${application.company_name} - ${application.position_name}`,
  }));

  const handleApplicationChange = (applicationId?: number) => {
    if (editing || applicationId === undefined) return;
    const application = applications.find((item) => item.id === applicationId);
    if (!application) return;

    const patch: Partial<Pick<OfferInput, 'company_name' | 'position_name'>> = {};
    const current = form.getFieldsValue(['company_name', 'position_name']);
    (['company_name', 'position_name'] as const).forEach((field) => {
      const currentValue = current[field];
      if (!currentValue?.trim() || currentValue === autoFilledApplicationFields.current[field]) {
        patch[field] = application[field];
        autoFilledApplicationFields.current[field] = application[field];
      }
    });
    form.setFieldsValue(patch);
  };
  const editingApplicationId = editing?.application_id;
  if (
    editing
    && listOfferBindingState(editing) === 'bound'
    && editingApplicationId !== undefined
    && !applicationOptions.some((option) => option.value === editingApplicationId)
  ) {
    applicationOptions.unshift({
      value: editingApplicationId,
      label: `#${editingApplicationId}（当前绑定投递不可见）`,
    });
  }

  const submit = (values: OfferInput) => {
    if (historicalReadOnly) return;
    if (!editing && (!Number.isInteger(values.application_id) || Number(values.application_id) <= 0)) {
      form.setFields([{ name: 'application_id', errors: ['请选择所属投递'] }]);
      return;
    }
    const payload: OfferInput = editing
      ? { ...values, application_id: editing.application_id }
      : { ...values, application_id: Number(values.application_id) };
    mutation.mutate(payload);
  };

  const mutation = useMutation({
    mutationFn: async (values: OfferInput) => {
      if (editing) return updateOffer(editing.id, values);
      return createOffer(values);
    },
    onSuccess: () => {
      toast.success(editing ? 'Offer 已更新' : 'Offer 已录入');
      qc.invalidateQueries({ queryKey: ['offers'] });
      onClose();
    },
    onError: (e: unknown) => {
      const msg = (e as { response?: { data?: { error?: string } } })?.response?.data?.error;
      toast.error(msg || '保存失败');
    },
  });

  return (
    <Modal
      title={historicalReadOnly ? '查看 Offer' : editing ? '编辑 Offer' : '录入 Offer'}
      open={open}
      onCancel={onClose}
      onOk={historicalReadOnly ? undefined : () => form.submit()}
      footer={historicalReadOnly ? <Button onClick={onClose}>关闭</Button> : undefined}
      confirmLoading={mutation.isPending}
      destroyOnHidden
      data-request-token={requestToken ?? undefined}
    >
      {historicalReadOnly ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="历史 Offer 仅支持只读查看"
          description="该记录没有所属投递，不能编辑、补绑定或进入谈薪准备。"
        />
      ) : null}
      <Form
        form={form}
        layout="vertical"
        disabled={historicalReadOnly}
        data-offer-mode={historicalReadOnly ? 'read-only' : 'editable'}
        onFinish={(v) => submit(v as OfferInput)}
      >
        <Form.Item name="company_name" label="公司" rules={[{ required: true, message: '请输入公司' }]}>
          <Input />
        </Form.Item>
        <Form.Item name="position_name" label="岗位" rules={[{ required: true, message: '请输入岗位' }]}>
          <Input />
        </Form.Item>
        <Form.Item
          name="application_id"
          label="关联投递"
          rules={editing ? undefined : [{ required: true, message: '请选择所属投递' }]}
        >
          <Select
            disabled={Boolean(editing)}
            allowClear={!editing}
            placeholder="选择投递记录"
            options={applicationOptions}
            onChange={handleApplicationChange}
          />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select options={STATUS_OPTIONS} />
        </Form.Item>
        <Form.Item name="base_monthly" label="月薪（元）" rules={[{ type: 'number', min: 0, message: '不能为负' }]}>
          <InputNumber style={{ width: '100%' }} min={0} />
        </Form.Item>
        <Form.Item name="months_per_year" label="薪数（如 12/13/16）" rules={[{ type: 'number', min: 1, message: '至少 1' }]}>
          <InputNumber style={{ width: '100%' }} min={1} />
        </Form.Item>
        <Form.Item name="signing_bonus" label="签字费（元）">
          <InputNumber style={{ width: '100%' }} min={0} />
        </Form.Item>
        <Form.Item name="equity" label="期权">
          <Input placeholder="如 20万股 RSU 4年 vest" />
        </Form.Item>
        <Form.Item name="perks" label="福利">
          <Input />
        </Form.Item>
        <Form.Item name="deadline" label="截止日">
          <Input placeholder="如 2026-07-08" />
        </Form.Item>
        <Form.Item name="notes" label="备注">
          <Input.TextArea rows={2} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
