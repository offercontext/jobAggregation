import type {
  CopyResumeInput,
  CreateResumeFromSampleInput,
  CreateResumeInput,
  MatchResumeResponse,
  Resume,
  UpdateResumeInput,
  ResumeImportPreview,
  ConfirmResumeImportInput,
} from '@/types/resume';
import { createApiClient } from './http';

const http = createApiClient({ baseURL: '/api', timeout: 130000 });

export async function createResume(input: CreateResumeInput): Promise<Resume> {
  const { data } = await http.post<Resume>('/resumes', input);
  return data;
}

export async function listResumes(): Promise<Resume[]> {
  const { data } = await http.get<Resume[]>('/resumes');
  return data;
}

export async function getResume(id: number, signal?: AbortSignal): Promise<Resume> {
  return (await http.get<Resume>(`/resumes/${id}`, { signal })).data;
}

export async function previewResumeStructure(id: number, signal?: AbortSignal): Promise<ResumeImportPreview> {
  return (await http.post<ResumeImportPreview>(`/resumes/${id}/structure-preview`, {}, { signal })).data;
}

export async function confirmResumeStructure(id: number, input: ConfirmResumeImportInput, signal?: AbortSignal): Promise<Resume> {
  return (await http.post<Resume>(`/resumes/${id}/structure-confirm`, input, { signal })).data;
}

export async function createResumeFromSample(input: CreateResumeFromSampleInput): Promise<Resume> {
  const { data } = await http.post<Resume>('/resumes/from-sample', input);
  return data;
}

export async function updateResume(id: number, input: UpdateResumeInput): Promise<Resume> {
  const { data } = await http.patch<Resume>(`/resumes/${id}`, input);
  return data;
}

export async function copyResume(id: number, input: CopyResumeInput = {}): Promise<Resume> {
  const { data } = await http.post<Resume>(`/resumes/${id}/copy`, input);
  return data;
}

export async function deleteResume(id: number): Promise<void> {
  await http.delete(`/resumes/${id}`);
}

export async function matchResume(
  resumeID: number,
  payload: { jd_text?: string; jd_url?: string; application_id?: number },
): Promise<MatchResumeResponse> {
  const { data } = await http.post<MatchResumeResponse>(`/resumes/${resumeID}/match`, payload);
  return data;
}

export async function uploadResume(file: File): Promise<Resume> {
  const formData = new FormData();
  formData.append('file', file);
  const { data } = await http.post<Resume>('/resumes/upload', formData, {
    timeout: 30000,
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}
