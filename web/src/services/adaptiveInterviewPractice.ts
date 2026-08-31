import axios from 'axios';
import type {
  AdaptivePracticeCompleteInput,
  AdaptivePracticePlan,
  AdaptivePracticeRecommendation,
  AdaptivePracticeV2StartInput,
} from '@/types/adaptiveInterviewPractice';
import { createApiClient } from './http';

const http = createApiClient({ baseURL: '/api', timeout: 15000 });

const COPY: Record<string, string> = {
  adaptive_practice_not_found: '练习或来源已不可见，请重新打开页面。',
  adaptive_practice_source_conflict: '复盘来源已变化，请重新核对后再开始。',
  adaptive_practice_idempotency_conflict: '本次操作内容已变化，请重新开始。',
  adaptive_practice_revision_conflict: '练习状态已变化，请重新加载。',
  adaptive_practice_invalid_payload: '练习内容不完整，请检查后重试。',
};

export class AdaptivePracticeError extends Error {
  constructor(public readonly code?: string) {
    super(code && COPY[code] ? COPY[code] : '复盘训练暂时不可用，请稍后重试。');
    this.name = 'AdaptivePracticeError';
  }
}

function safeError(error: unknown): AdaptivePracticeError {
  const data = axios.isAxiosError(error) ? error.response?.data : undefined;
  const code = typeof data?.error_code === 'string' ? data.error_code : undefined;
  return new AdaptivePracticeError(code);
}

export async function listAdaptivePracticeRecommendations(): Promise<AdaptivePracticeRecommendation[]> {
  try {
    return (await http.get('/interview-practice/recommendations')).data;
  } catch (error) {
    throw safeError(error);
  }
}

export async function listAdaptivePracticePlans(): Promise<AdaptivePracticePlan[]> {
  try {
    return (await http.get('/interview-practice/plans')).data;
  } catch (error) {
    throw safeError(error);
  }
}

export async function startAdaptivePracticeV2(
  input: AdaptivePracticeV2StartInput,
): Promise<AdaptivePracticePlan> {
  try {
    return (await http.post('/interview-practice/plans', input)).data;
  } catch (error) {
    throw safeError(error);
  }
}

export async function completeAdaptivePractice(
  planId: number,
  input: AdaptivePracticeCompleteInput,
): Promise<AdaptivePracticePlan> {
  try {
    return (await http.post(`/interview-practice/plans/${planId}/complete`, input)).data;
  } catch (error) {
    throw safeError(error);
  }
}
