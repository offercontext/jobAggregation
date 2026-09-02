import { describe, expect, it } from 'vitest';
import { buildDiagnosticsText } from './diagnostics';
import type { LogEntry, Settings } from '@/services/chat';

const settings: Settings = {
  version: '0.1.0',
  data_dir: '/tmp/offerpilot',
  chat_auto_approve_writes: false,
  active_provider_id: 'local',
  fallback_provider_ids: [],
  providers: [
    {
      id: 'local',
      label: '本地模型',
      provider: 'openai_compatible',
      base_url: 'http://127.0.0.1:4010/v1',
      model: 'stub',
      enabled: true,
      supports_json_schema: false,
      context_window: 128_000,
      max_output_tokens: 4_096,
      has_api_key: true,
    },
  ],
  base_url: 'http://127.0.0.1:4010/v1',
  model: 'stub',
  has_api_key: true,
  runtime_mode: 'local',
  auth_enabled: false,
  has_auth_token: true,
  log_level: 'INFO',
};

describe('buildDiagnosticsText', () => {
  it('copies runtime facts without credentials or business data', () => {
    const logs: LogEntry[] = [{ level: 'ERROR', message: 'provider failed' }];
    const text = buildDiagnosticsText(settings, logs);

    expect(text).toContain('数据目录: /tmp/offerpilot');
    expect(text).toContain('ERROR provider failed');
    expect(text).not.toContain('sk-secret');
    expect(text).not.toContain('auth-secret');
    expect(text).not.toContain('127.0.0.1:4010');
  });
});
