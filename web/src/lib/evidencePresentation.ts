import { storySourceLabel } from './storySourcePresentation';

export function resumeEvidenceLocation(path: string): string {
  const relative = path.replace(/^\/resume(?=\/)/, '');
  return storySourceLabel('resume_version', relative.startsWith('/content_json/') ? relative : `/content_json${relative}`);
}

export function evidenceSourceLabel(source: string): string {
  const labels: Record<string, string> = {
    jd: '岗位描述',
    resume: '选定简历',
    knowledge_evidence: '已确认知识依据',
    confirmed_readiness_feedback: '已确认复盘重点',
    turn: '先前回答',
    user_assertion: '我的补充说明',
    evidence_bundle: '投递参考内容',
  };
  return Object.prototype.hasOwnProperty.call(labels, source) ? labels[source] : '参考依据';
}

export function evidenceLocationLabel(source: string, path: string): string {
  if (source === 'resume' || (source === 'evidence_bundle' && path.startsWith('/resume/'))) return resumeEvidenceLocation(path);
  if (source === 'jd' || (source === 'evidence_bundle' && path === '/jd/text')) return '岗位要求';
  const turn = /^\/turns\/(\d+)\/answer$/.exec(path);
  if (source === 'turn') return turn ? `第 ${Number(turn[1])} 轮回答` : '回答片段';
  const assertion = /^\/user_assertions\/(\d+)\/text$/.exec(path);
  if (source === 'user_assertion') return assertion ? `我的补充说明 ${Number(assertion[1]) + 1}` : '我的补充说明';
  if (source === 'confirmed_readiness_feedback') {
    const feedback = /^\/readiness_feedback\/(\d+)\/(?:statement|evidence\/(\d+)\/excerpt)$/.exec(path);
    return feedback ? `复盘准备重点 ${Number(feedback[1]) + 1}${feedback[2] === undefined ? '' : ` · 证据 ${Number(feedback[2]) + 1}`}` : '准备重点片段';
  }
  return source === 'knowledge_evidence' ? '知识证据片段' : '来源片段';
}
