import type {
  ChatMessage,
  ChatResponse,
  ChatStreamEvent,
  Conversation,
  PilotActionRequest,
  PilotContextAttachment,
  PilotPageContext,
} from '@/types/chat';
import { authHeaders } from './authToken';
import { createApiClient } from './http';

const http = createApiClient({ baseURL: '/api', timeout: 130000 });
export const SETTINGS_QUERY_KEY = ['settings'] as const;

export interface ChatContextInput {
  context_type?: 'workspace' | 'application' | 'global' | string;
  context_ref?: string | number;
  mode?: string;
  page_context?: PilotPageContext;
  attachments?: PilotContextAttachment[];
  pilot_action?: PilotActionRequest;
}

export interface ChatRequestOptions {
  signal?: AbortSignal;
}

export interface ChatStreamRequestOptions extends ChatRequestOptions {
  onEvent?: (event: ChatStreamEvent) => void;
}

export class ChatStreamError extends Error {
  code?: string;
  retryable?: boolean;

  constructor(message: string, code?: string, retryable?: boolean) {
    super(message);
    this.name = 'ChatStreamError';
    this.code = code;
    this.retryable = retryable;
  }
}

export type ConfirmationInput =
  | {
      approved: true;
      operation_id?: string;
      confirmation_token: string;
      edited_args?: Record<string, unknown>;
      rejection_feedback?: never;
    }
  | {
      approved: false;
      operation_id?: string;
      confirmation_token: string;
      rejection_feedback?: string;
      edited_args?: never;
    };

export type ConfirmationRequest = ConfirmationInput | boolean;

function confirmationPayload(input: ConfirmationRequest): ConfirmationInput | { approved: boolean } {
  return typeof input === 'boolean' ? { approved: input } : input;
}

export async function sendChat(
  message: string,
  conversationId?: number,
  context?: ChatContextInput,
  options?: ChatRequestOptions,
): Promise<ChatResponse> {
  const { data } = await http.post<ChatResponse>(
    '/chat',
    {
      message,
      conversation_id: conversationId ?? 0,
      ...(context?.context_type ? { context_type: context.context_type } : {}),
      ...(context?.context_ref !== undefined ? { context_ref: String(context.context_ref) } : {}),
      ...(context?.mode ? { mode: context.mode } : {}),
      ...(context?.page_context !== undefined ? { page_context: context.page_context } : {}),
      ...(context?.attachments !== undefined ? { attachments: context.attachments } : {}),
      ...(context?.pilot_action !== undefined ? { pilot_action: context.pilot_action } : {}),
    },
    { signal: options?.signal },
  );
  return data;
}

export async function confirmAction(
  conversationId: number,
  input: ConfirmationRequest,
  options?: ChatRequestOptions,
): Promise<ChatResponse> {
  const { data } = await http.post<ChatResponse>(
    '/chat/confirm',
    {
      conversation_id: conversationId,
      ...confirmationPayload(input),
    },
    { signal: options?.signal },
  );
  return data;
}

export async function undoLastWrite(
  conversationId: number,
  parentOperationId?: string,
  options?: ChatRequestOptions,
): Promise<Extract<ChatResponse, { type: 'message' }>> {
  const { data } = await http.post<Extract<ChatResponse, { type: 'message' }>>(
    '/chat/undo-last-write',
    {
      conversation_id: conversationId,
      ...(parentOperationId ? { parent_operation_id: parentOperationId } : {}),
    },
    { signal: options?.signal },
  );
  return data;
}

export function createSseParser(onEvent: (event: ChatStreamEvent) => void) {
  let buffer = '';

  function parseFrame(frame: string) {
    const dataLines: string[] = [];
    let eventName = '';
    for (const line of frame.split('\n')) {
      if (line.startsWith(':')) continue;
      if (line.startsWith('event:')) {
        eventName = line.slice('event:'.length).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice('data:'.length).trim());
      }
    }
    if (!dataLines.length) return;
    const parsed = JSON.parse(dataLines.join('\n')) as ChatStreamEvent;
    onEvent({ ...parsed, event: parsed.event || eventName });
  }

  return {
    push(chunk: string) {
      buffer += chunk;
      buffer = buffer.replace(/\r\n/g, '\n');
      let frameEnd = buffer.indexOf('\n\n');
      while (frameEnd >= 0) {
        const frame = buffer.slice(0, frameEnd).trim();
        buffer = buffer.slice(frameEnd + 2);
        if (frame) parseFrame(frame);
        frameEnd = buffer.indexOf('\n\n');
      }
    },
    flush() {
      const frame = buffer.trim();
      buffer = '';
      if (frame) parseFrame(frame);
    },
  };
}

export async function streamChat(
  message: string,
  conversationId?: number,
  context?: ChatContextInput,
  options?: ChatStreamRequestOptions,
): Promise<ChatResponse> {
  return postChatStream(
    '/api/chat/stream',
    {
      message,
      conversation_id: conversationId ?? 0,
      ...(context?.context_type ? { context_type: context.context_type } : {}),
      ...(context?.context_ref !== undefined ? { context_ref: String(context.context_ref) } : {}),
      ...(context?.mode ? { mode: context.mode } : {}),
      ...(context?.page_context !== undefined ? { page_context: context.page_context } : {}),
      ...(context?.attachments !== undefined ? { attachments: context.attachments } : {}),
      ...(context?.pilot_action !== undefined ? { pilot_action: context.pilot_action } : {}),
    },
    options,
  );
}

export async function streamConfirmAction(
  conversationId: number,
  input: ConfirmationRequest,
  options?: ChatStreamRequestOptions,
): Promise<ChatResponse> {
  return postChatStream(
    '/api/chat/confirm/stream',
    {
      conversation_id: conversationId,
      ...confirmationPayload(input),
    },
    options,
  );
}

async function postChatStream(
  url: string,
  body: Record<string, unknown>,
  options?: ChatStreamRequestOptions,
): Promise<ChatResponse> {
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
    },
    body: JSON.stringify(body),
    signal: options?.signal,
  });
  if (!response.ok) {
    throw await streamHttpError(response);
  }
  if (!response.body) {
    throw new Error('对话连接中断，请稍后重试。');
  }

  let completed: ChatResponse | undefined;
  const decoder = new TextDecoder();
  const parser = createSseParser((event) => {
    options?.onEvent?.(event);
    if (event.event === 'completed') {
      const data = event.data as { response?: ChatResponse };
      completed = data.response;
    }
    if (event.event === 'error') {
      const data = event.data as { code?: string; message?: string; retryable?: boolean };
      throw new ChatStreamError(
        data.message || '对话失败，请稍后重试',
        data.code,
        data.retryable,
      );
    }
  });
  const reader = response.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    parser.push(decoder.decode(value, { stream: true }));
  }
  parser.push(decoder.decode());
  parser.flush();
  if (!completed) {
    throw new Error('对话没有返回完整结果，请重试。');
  }
  return completed;
}

async function streamHttpError(response: Response) {
  try {
    const payload = (await response.json()) as { error?: string };
    return new ChatStreamError(payload.error || `HTTP ${response.status}`, `http_${response.status}`);
  } catch {
    return new ChatStreamError(`HTTP ${response.status}`, `http_${response.status}`);
  }
}

export async function listConversations(includeArchived = false): Promise<Conversation[]> {
  const { data } = await http.get<Conversation[]>('/chat/conversations', {
    params: includeArchived ? { include_archived: true } : undefined,
  });
  return data ?? [];
}

export async function getConversation(id: number): Promise<ChatMessage[]> {
  const { data } = await http.get<ChatMessage[]>(`/chat/conversations/${id}`);
  return data ?? [];
}

export async function deleteConversation(id: number): Promise<void> {
  await http.delete(`/chat/conversations/${id}`);
}

export interface UpdateConversationPayload {
  title?: string;
  pinned?: boolean;
  archived?: boolean;
  context_type?: string;
  context_ref?: string;
}

export async function updateConversation(
  id: number,
  payload: UpdateConversationPayload,
): Promise<Conversation> {
  const { data } = await http.patch<Conversation>(`/chat/conversations/${id}`, payload);
  return data;
}

export interface Settings {
  version: string;
  data_dir: string;
  chat_auto_approve_writes: boolean;
  active_provider_id: string;
  fallback_provider_ids: string[];
  providers: AIProviderProfile[];
  base_url: string;
  model: string;
  has_api_key: boolean;
  runtime_mode: 'local' | 'server';
  auth_enabled: boolean;
  has_auth_token: boolean;
  log_level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';
}

export interface LogEntry {
  level: string;
  message: string;
}

export interface LogsPage {
  entries: LogEntry[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface AIProviderProfile {
  id: string;
  label: string;
  provider: string;
  base_url: string;
  model: string;
  enabled: boolean;
  supports_json_schema: boolean;
  context_window: number;
  max_output_tokens: number;
  has_api_key: boolean;
}

export interface UpdateSettingsPayload {
  chat_auto_approve_writes: boolean;
  active_provider_id?: string;
  fallback_provider_ids?: string[];
  providers?: Array<Omit<AIProviderProfile, 'has_api_key'> & { api_key?: string }>;
  base_url?: string;
  model?: string;
  api_key?: string;
}

export interface ProviderConnectionTestPayload {
  provider_id?: string;
  provider?: Omit<AIProviderProfile, 'has_api_key'> & { api_key?: string };
}

export interface ProviderConnectionTestResult {
  ok: boolean;
  provider_id?: string;
  model?: string;
  latency_ms?: number;
  message?: string;
  error?: string;
}

export interface SettingsBackup {
  version: number;
  exported_at: string;
  runtime_mode: Settings['runtime_mode'];
  auth_enabled: boolean;
  has_auth_token: boolean;
  log_level: Settings['log_level'];
  chat_auto_approve_writes: boolean;
  active_provider_id: string;
  fallback_provider_ids: string[];
  providers: AIProviderProfile[];
}

export async function getSettings(): Promise<Settings> {
  const { data } = await http.get<Settings>('/settings');
  return data;
}

export async function updateSettings(payload: UpdateSettingsPayload): Promise<Settings> {
  const { data } = await http.put<Settings>('/settings', payload);
  return data;
}

export async function testProviderConnection(
  payload: ProviderConnectionTestPayload,
): Promise<ProviderConnectionTestResult> {
  const { data } = await http.post<ProviderConnectionTestResult>('/settings/providers/test', payload);
  return data;
}

export async function getSettingsBackup(): Promise<SettingsBackup> {
  const { data } = await http.get<SettingsBackup>('/settings/backup');
  return data;
}

export async function exportBackup(path = '/backups/export'): Promise<void> {
  const { data } = await http.get<Blob>(path, { responseType: 'blob' });
  const archive = data instanceof Blob ? data : new Blob([data], { type: 'application/zip' });
  const url = URL.createObjectURL(archive);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'offerpilot-backup.zip';
  anchor.click();
  URL.revokeObjectURL(url);
}

export async function getLogs(limit = 20, offset = 0, level = ''): Promise<LogsPage> {
  const { data } = await http.get<LogsPage>('/logs', {
    params: { limit, offset, ...(level ? { level } : {}) },
  });
  return {
    entries: data.entries ?? [],
    total: data.total ?? 0,
    limit: data.limit ?? limit,
    offset: data.offset ?? offset,
    has_more: data.has_more ?? false,
  };
}

export async function updateAutoApprove(value: boolean): Promise<Settings> {
  const current = await getSettings();
  return updateSettings({
    chat_auto_approve_writes: value,
    base_url: current.base_url,
    model: current.model,
  });
}
