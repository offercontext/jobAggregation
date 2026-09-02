import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Divider,
  Form,
  Input,
  InputNumber,
  List,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  ApiOutlined,
  ArrowLeftOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  KeyOutlined,
  PlusOutlined,
  StarOutlined,
  SyncOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import {
  getSettings,
  SETTINGS_QUERY_KEY,
  testProviderConnection,
  updateSettings,
  type AIProviderProfile,
  type ProviderConnectionTestResult,
  type Settings,
  type UpdateSettingsPayload,
} from '@/services/chat';
import { ONBOARDING_QUERY_KEY } from '@/services/onboarding';
import workflowStyles from './ui/WorkflowSurface.module.css';

interface Props {
  open: boolean;
  onClose: () => void;
}

type EditableProvider = Omit<AIProviderProfile, 'has_api_key'> & {
  api_key?: string;
  has_api_key?: boolean;
};

interface FormValues {
  id: string;
  label: string;
  api_key?: string;
  provider: string;
  base_url: string;
  model: string;
  context_window: number;
  max_output_tokens: number;
  enabled: boolean;
  supports_json_schema: boolean;
  chat_auto_approve_writes: boolean;
}

const PROVIDER_LABELS: Record<string, string> = {
  openai: 'OpenAI',
  openai_compatible: 'OpenAI 兼容',
  anthropic: 'Anthropic',
  openrouter: 'OpenRouter',
};

const PROVIDER_OPTIONS = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'openai_compatible', label: 'OpenAI 兼容' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openrouter', label: 'OpenRouter' },
];

const DEFAULT_BASE_URL = 'https://api.openai.com/v1';
const DEFAULT_MODEL = 'gpt-4o';
const PROVIDER_FRAMING_RESERVE = 1_024;

export default function AISettingsDrawer({ open, onClose }: Props) {
  const [form] = Form.useForm<FormValues>();
  const qc = useQueryClient();
  const [providers, setProviders] = useState<EditableProvider[]>([]);
  const [activeProviderId, setActiveProviderId] = useState('default');
  const [fallbackProviderIds, setFallbackProviderIds] = useState<string[]>([]);
  const [editingId, setEditingId] = useState('default');
  const [connectionResult, setConnectionResult] = useState<ProviderConnectionTestResult | null>(null);

  const settingsQuery = useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: getSettings,
    enabled: open,
  });

  const activeProvider = useMemo(
    () => providers.find((item) => item.id === activeProviderId) ?? providers[0],
    [activeProviderId, providers],
  );

  useEffect(() => {
    if (!open || !settingsQuery.data) return;
    const nextProviders = toEditableProviders(settingsQuery.data);
    const nextActiveId = settingsQuery.data.active_provider_id || nextProviders[0]?.id || 'default';
    setProviders(nextProviders);
    setActiveProviderId(nextActiveId);
    setFallbackProviderIds(settingsQuery.data.fallback_provider_ids ?? []);
    setEditingId(nextActiveId);
    setConnectionResult(null);
    form.setFieldsValue(providerToForm(nextProviders.find((item) => item.id === nextActiveId), settingsQuery.data));
  }, [form, open, settingsQuery.data]);

  const saveMutation = useMutation({
    mutationFn: async () => {
      const values = await form.validateFields();
      const nextProviders = upsertProvider(providers, formToProvider(values, providers.find((item) => item.id === values.id)));
      const nextActiveId = nextProviders.some((item) => item.id === activeProviderId)
        ? activeProviderId
        : nextProviders[0]?.id || 'default';
      const nextFallbackIds = Array.from(
        new Set(
          fallbackProviderIds.filter(
            (providerId) => providerId !== nextActiveId && nextProviders.some((item) => item.id === providerId),
          ),
        ),
      );
      const incompleteProvider = nextProviders.find(
        (provider) => providerRequiresValidBudget(provider, nextActiveId, nextFallbackIds)
          && !hasValidProviderBudget(provider),
      );
      if (incompleteProvider) {
        throw new Error(`请先补全 ${incompleteProvider.label || incompleteProvider.id} 的模型预算配置`);
      }
      const nextActive = nextProviders.find((item) => item.id === nextActiveId) ?? nextProviders[0];
      setProviders(nextProviders);
      setActiveProviderId(nextActiveId);
      setFallbackProviderIds(nextFallbackIds);
      const payload: UpdateSettingsPayload = {
        chat_auto_approve_writes: values.chat_auto_approve_writes,
        active_provider_id: nextActiveId,
        fallback_provider_ids: nextFallbackIds,
        base_url: nextActive?.base_url || DEFAULT_BASE_URL,
        model: nextActive?.model || DEFAULT_MODEL,
        providers: nextProviders.map(providerPayload),
      };
      return updateSettings(payload);
    },
    onSuccess: (settings) => {
      qc.setQueryData(SETTINGS_QUERY_KEY, settings);
      qc.invalidateQueries({ queryKey: SETTINGS_QUERY_KEY });
      qc.invalidateQueries({ queryKey: ONBOARDING_QUERY_KEY });
      message.success('AI 设置已保存');
      setProviders(toEditableProviders(settings));
      setActiveProviderId(settings.active_provider_id);
      setFallbackProviderIds(settings.fallback_provider_ids ?? []);
      form.setFieldValue('api_key', '');
      onClose();
    },
    onError: (error) => {
      message.error(error instanceof Error ? error.message : 'AI 设置保存失败');
    },
  });

  const testMutation = useMutation({
    mutationFn: async () => {
      const values = await form.validateFields();
      const existing = providers.find((item) => item.id === values.id);
      const provider = formToProvider(values, existing);
      const payload =
        existing?.has_api_key && !provider.api_key && providerMatchesSavedFields(provider, existing)
          ? { provider_id: values.id }
          : { provider: providerPayload(provider) };
      return testProviderConnection(payload);
    },
    onSuccess: (result) => {
      setConnectionResult(result);
      if (result.ok) {
        message.success(result.message || '连接成功');
      } else {
        message.error(result.error || '连接失败');
      }
    },
    onError: () => {
      setConnectionResult({ ok: false, error: '连接失败' });
      message.error('连接失败');
    },
  });

  const hasKey = Boolean(activeProvider?.has_api_key);
  const hasBudget = Boolean(activeProvider && hasValidProviderBudget(activeProvider));

  function startNewProvider() {
    const id = uniqueProviderId('provider', providers);
    const provider: EditableProvider = {
      id,
      label: '新供应商',
      provider: 'openai',
      base_url: DEFAULT_BASE_URL,
      model: DEFAULT_MODEL,
      context_window: 0,
      max_output_tokens: 0,
      enabled: true,
      supports_json_schema: false,
      api_key: '',
      has_api_key: false,
    };
    setEditingId(id);
    setConnectionResult(null);
    form.setFieldsValue(providerToForm(provider, settingsQuery.data));
  }

  function editProvider(provider: EditableProvider) {
    setEditingId(provider.id);
    setConnectionResult(null);
    form.setFieldsValue(providerToForm(provider, settingsQuery.data));
  }

  async function applyProviderForm() {
    const values = await form.validateFields();
    const nextProvider = formToProvider(values, providers.find((item) => item.id === values.id));
    setProviders((current) => upsertProvider(current, nextProvider));
    setEditingId(nextProvider.id);
    message.success('供应商已更新');
  }

  function removeProvider(providerId: string) {
    if (providerId === activeProviderId) return;
    const nextProviders = providers.filter((item) => item.id !== providerId);
    setProviders(nextProviders);
    setFallbackProviderIds((current) => current.filter((item) => item !== providerId));
    if (editingId === providerId) {
      const nextProvider = nextProviders.find((item) => item.id === activeProviderId) ?? nextProviders[0];
      if (nextProvider) {
        editProvider(nextProvider);
      }
    }
  }

  function markDefault(provider: EditableProvider) {
    if (!hasValidProviderBudget(provider)) {
      message.warning('请先补全该供应商的模型预算配置');
      return;
    }
    setActiveProviderId(provider.id);
    setFallbackProviderIds((current) => current.filter((item) => item !== provider.id));
  }

  if (!open) return null;

  return (
    <section aria-label="AI 设置" className={`${workflowStyles.surface} ${workflowStyles.stack}`} style={{ maxWidth: 1040 }}>
      <div className={workflowStyles.sectionHeader}>
        <div>
        <Button
          type="link"
          icon={<ArrowLeftOutlined />}
          onClick={onClose}
          style={{ width: 'fit-content', height: 'auto', padding: 0 }}
        >
          返回设置
        </Button>
          <Typography.Title level={3} style={{ margin: '8px 0 0', color: 'var(--op-ink)' }}>
            <ApiOutlined /> AI 设置
          </Typography.Title>
        </div>
          <Button type="primary" loading={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            保存
          </Button>
      </div>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Alert
          type={hasKey && hasBudget ? 'success' : 'warning'}
          showIcon
          icon={hasKey && hasBudget ? <CheckCircleOutlined /> : <WarningOutlined />}
          message={!hasKey ? '尚未配置密钥' : hasBudget ? 'AI 配置已就绪' : '预算配置待补全'}
          description={
            !hasKey
              ? '配置模型供应商密钥后即可使用 AI 能力。'
              : hasBudget
                ? '密钥留空保存会保留当前密钥。'
                : '请按当前模型的官方规格填写上下文窗口和单次最大输出。'
          }
        />

        <section data-testid="ai-provider-list" className={workflowStyles.section}>
          <Space align="center" style={{ width: '100%', justifyContent: 'space-between' }}>
            <Typography.Title level={5} style={{ margin: 0 }}>
              Provider 列表
            </Typography.Title>
            <Button icon={<PlusOutlined />} onClick={startNewProvider}>
              新增供应商
            </Button>
          </Space>
          <List
            size="small"
            dataSource={providers}
            locale={{ emptyText: '暂无供应商' }}
            renderItem={(provider) => (
              <List.Item
                className={workflowStyles.listRow}
                actions={[
                  <Button key="edit" size="small" icon={<EditOutlined />} onClick={() => editProvider(provider)}>
                    编辑供应商
                  </Button>,
                  <Button
                    key="default"
                    size="small"
                    icon={<StarOutlined />}
                    disabled={provider.id === activeProviderId || !hasValidProviderBudget(provider)}
                    onClick={() => markDefault(provider)}
                  >
                    设为默认
                  </Button>,
                  <Popconfirm
                    key="delete"
                    title="删除供应商"
                    okText="删除"
                    cancelText="取消"
                    disabled={provider.id === activeProviderId}
                    onConfirm={() => removeProvider(provider.id)}
                  >
                    <Button size="small" danger icon={<DeleteOutlined />} disabled={provider.id === activeProviderId}>
                      删除供应商
                    </Button>
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space wrap>
                      <Typography.Text strong>{provider.label || provider.id}</Typography.Text>
                      {provider.id === activeProviderId ? <Tag color="blue">默认</Tag> : null}
                      {fallbackProviderIds.includes(provider.id) ? <Tag color="gold">Fallback</Tag> : null}
                      {provider.has_api_key ? <Tag color="green">密钥已配置</Tag> : <Tag>未配置密钥</Tag>}
                      {provider.enabled && !hasValidProviderBudget(provider) ? (
                        <Tag color="orange">预算配置待补全</Tag>
                      ) : null}
                    </Space>
                  }
                  description={`${PROVIDER_LABELS[provider.provider] || provider.provider} · ${provider.model}`}
                />
              </List.Item>
            )}
          />
        </section>

        <Divider style={{ margin: 0 }} />

        <Form<FormValues>
          form={form}
          layout="vertical"
          initialValues={providerToForm(activeProvider, settingsQuery.data)}
          className={workflowStyles.section}
        >
          <Typography.Title level={5} style={{ marginTop: 0 }}>
            编辑供应商
          </Typography.Title>

          <Form.Item label="配置 ID" name="id" rules={[{ required: true, message: '请输入配置 ID' }]}>
            <Input placeholder="openai" disabled={providers.some((item) => item.id === editingId)} />
          </Form.Item>

          <Form.Item label="显示名称" name="label" rules={[{ required: true, message: '请输入显示名称' }]}>
            <Input placeholder="OpenAI" />
          </Form.Item>

          <Form.Item label="API 密钥" name="api_key">
            <Input.Password
              prefix={<KeyOutlined />}
              autoComplete="off"
              placeholder={activeProvider?.has_api_key ? '留空则保留当前密钥' : 'sk-...'}
            />
          </Form.Item>

          <Form.Item label="模型供应商" name="provider">
            <Select options={PROVIDER_OPTIONS} />
          </Form.Item>

          <Form.Item label="接口地址" name="base_url" tooltip="OpenAI 兼容接口地址">
            <Input placeholder={DEFAULT_BASE_URL} />
          </Form.Item>

          <Form.Item label="模型" name="model">
            <Input placeholder={DEFAULT_MODEL} />
          </Form.Item>

          <Form.Item
            label="上下文窗口（tokens）"
            name="context_window"
            required
            dependencies={['enabled', 'max_output_tokens']}
            tooltip="填写当前模型官方公布的上下文窗口，不会根据模型名称自动猜测。"
            rules={[
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (getFieldValue('enabled') !== true && (!value || value <= 0)) return Promise.resolve();
                  if (!Number.isInteger(value) || value <= 0) {
                    return Promise.reject(new Error('请输入大于 0 的整数上下文窗口'));
                  }
                  const output = getFieldValue('max_output_tokens');
                  if (Number.isInteger(output) && output > 0 && value <= output + PROVIDER_FRAMING_RESERVE) {
                    return Promise.reject(new Error('上下文窗口必须大于最大输出与协议预留之和'));
                  }
                  return Promise.resolve();
                },
              }),
            ]}
          >
            <InputNumber min={1} precision={0} step={1_024} style={{ width: '100%' }} placeholder="例如 131072" />
          </Form.Item>

          <Form.Item
            label="单次最大输出（tokens）"
            name="max_output_tokens"
            required
            dependencies={['enabled', 'context_window']}
            tooltip="填写每次请求允许模型生成的最大 token 数。"
            rules={[
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (getFieldValue('enabled') !== true && (!value || value <= 0)) return Promise.resolve();
                  if (!Number.isInteger(value) || value <= 0) {
                    return Promise.reject(new Error('请输入大于 0 的整数最大输出'));
                  }
                  const contextWindow = getFieldValue('context_window');
                  if (
                    Number.isInteger(contextWindow)
                    && contextWindow > 0
                    && contextWindow <= value + PROVIDER_FRAMING_RESERVE
                  ) {
                    return Promise.reject(new Error('单次最大输出必须为上下文和协议预留留出空间'));
                  }
                  return Promise.resolve();
                },
              }),
            ]}
          >
            <InputNumber min={1} precision={0} step={1_024} style={{ width: '100%' }} placeholder="例如 8192" />
          </Form.Item>

          <Form.Item label="启用" name="enabled" valuePropName="checked">
            <Switch />
          </Form.Item>

          <Form.Item
            label="原生 JSON Schema"
            name="supports_json_schema"
            valuePropName="checked"
            tooltip="仅在 Provider 明确支持时启用"
          >
            <Switch />
          </Form.Item>

          <Form.Item label="Fallback 供应商">
            <Select
              mode="multiple"
              placeholder="默认供应商失败时依次尝试"
              value={fallbackProviderIds}
              onChange={(value: string[]) => setFallbackProviderIds(value)}
              options={providers
                .filter((provider) => provider.id !== activeProviderId)
                .map((provider) => ({
                  value: provider.id,
                  label: provider.label || provider.id,
                  disabled: !hasValidProviderBudget(provider),
                }))}
            />
          </Form.Item>

          <Form.Item label="写操作自动确认" name="chat_auto_approve_writes" valuePropName="checked">
            <Switch />
          </Form.Item>

          <Space wrap className={workflowStyles.actionGroup}>
            <Button onClick={applyProviderForm}>应用到 Provider 列表</Button>
            <Button icon={<SyncOutlined />} loading={testMutation.isPending} onClick={() => testMutation.mutate()}>
              测试连接
            </Button>
          </Space>

          {connectionResult ? (
            <Alert
              style={{ marginTop: 12 }}
              type={connectionResult.ok ? 'success' : 'error'}
              showIcon
              message={connectionResult.ok ? connectionResult.message || '连接成功' : connectionResult.error || '连接失败'}
              description={
                connectionResult.ok && connectionResult.latency_ms !== undefined
                  ? `${connectionResult.model || ''} · ${connectionResult.latency_ms}ms`
                  : undefined
              }
            />
          ) : null}

          <Typography.Paragraph type="secondary" style={{ marginTop: 14, marginBottom: 0 }}>
            设置保存在本地 OfferPilot 配置中；接口只返回密钥是否存在。
          </Typography.Paragraph>

          <div style={{ marginTop: 14 }}>
            <Tag color="blue">LiteLLM</Tag>
            <Tag color="default">本地配置</Tag>
          </div>
        </Form>
      </Space>
    </section>
  );
}

function toEditableProviders(settings?: Settings): EditableProvider[] {
  const providers = settings?.providers?.length
    ? settings.providers
    : [
        {
          id: 'default',
          label: 'Default',
          provider: 'openai',
          base_url: DEFAULT_BASE_URL,
          model: DEFAULT_MODEL,
          context_window: 0,
          max_output_tokens: 0,
          enabled: true,
          supports_json_schema: false,
          has_api_key: false,
        },
      ];
  return providers.map((provider) => ({ ...provider, api_key: '' }));
}

function providerToForm(provider?: EditableProvider, settings?: Settings): FormValues {
  return {
    id: provider?.id || 'default',
    label: provider?.label || 'Default',
    api_key: '',
    provider: provider?.provider || 'openai',
    base_url: provider?.base_url || settings?.base_url || DEFAULT_BASE_URL,
    model: provider?.model || settings?.model || DEFAULT_MODEL,
    context_window: provider?.context_window ?? 0,
    max_output_tokens: provider?.max_output_tokens ?? 0,
    enabled: provider?.enabled ?? true,
    supports_json_schema: provider?.supports_json_schema ?? false,
    chat_auto_approve_writes: settings?.chat_auto_approve_writes ?? false,
  };
}

function formToProvider(values: FormValues, existing?: EditableProvider): EditableProvider {
  return {
    id: values.id.trim(),
    label: values.label.trim() || values.id.trim(),
    provider: values.provider,
    api_key: values.api_key?.trim() || '',
    base_url: values.base_url.trim() || DEFAULT_BASE_URL,
    model: values.model.trim() || DEFAULT_MODEL,
    context_window: values.context_window,
    max_output_tokens: values.max_output_tokens,
    enabled: values.enabled,
    supports_json_schema: values.supports_json_schema,
    has_api_key: Boolean(values.api_key?.trim() || existing?.has_api_key),
  };
}

function upsertProvider(providers: EditableProvider[], provider: EditableProvider): EditableProvider[] {
  const index = providers.findIndex((item) => item.id === provider.id);
  if (index === -1) return [...providers, provider];
  return providers.map((item) => (item.id === provider.id ? provider : item));
}

function providerPayload(provider: EditableProvider): Omit<AIProviderProfile, 'has_api_key'> & { api_key?: string } {
  const payload: Omit<AIProviderProfile, 'has_api_key'> & { api_key?: string } = {
    id: provider.id,
    label: provider.label,
    provider: provider.provider,
    base_url: provider.base_url,
    model: provider.model,
    context_window: provider.context_window,
    max_output_tokens: provider.max_output_tokens,
    enabled: provider.enabled,
    supports_json_schema: provider.supports_json_schema,
  };
  if (provider.api_key?.trim()) {
    payload.api_key = provider.api_key.trim();
  }
  return payload;
}

function providerMatchesSavedFields(provider: EditableProvider, saved: EditableProvider) {
  return (
    provider.provider === saved.provider &&
    provider.base_url === saved.base_url &&
    provider.model === saved.model &&
    provider.context_window === saved.context_window &&
    provider.max_output_tokens === saved.max_output_tokens &&
    provider.enabled === saved.enabled
  );
}

function hasValidProviderBudget(provider: Pick<AIProviderProfile, 'context_window' | 'max_output_tokens'>) {
  return (
    Number.isInteger(provider.context_window)
    && Number.isInteger(provider.max_output_tokens)
    && provider.context_window > provider.max_output_tokens + PROVIDER_FRAMING_RESERVE
    && provider.max_output_tokens > 0
  );
}

function providerRequiresValidBudget(
  provider: Pick<AIProviderProfile, 'id' | 'enabled'>,
  activeProviderId: string,
  fallbackProviderIds: string[],
) {
  return provider.enabled || provider.id === activeProviderId || fallbackProviderIds.includes(provider.id);
}

function uniqueProviderId(prefix: string, providers: EditableProvider[]) {
  let index = providers.length + 1;
  let id = `${prefix}-${index}`;
  while (providers.some((provider) => provider.id === id)) {
    index += 1;
    id = `${prefix}-${index}`;
  }
  return id;
}
