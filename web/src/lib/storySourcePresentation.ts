import type { InterviewStorySourceCandidates, InterviewStorySourceSelection } from '@/types/interviewStory';

export const STORY_SOURCE_KINDS = { resume_version: '我的简历', interview_note: '面试复盘', mock_turn: '模拟面试' } as const;
type Kind = InterviewStorySourceSelection['source_kind'];
export function storySourceLabel(kind: Kind, path: string): string {
  if (kind === 'mock_turn') {
    const match = /^\/turns\/(\d+)\/(question|answer)$/.exec(path);
    return match ? `第${Number(match[1])}题 · ${match[2] === 'question' ? '面试官提问' : '我的回答'}` : '其他内容';
  }
  if (kind === 'interview_note') return ({ '/questions': '面试问题', '/self_reflection': '我的复盘', '/difficulty_points': '遇到的难点', '/mood': '面试感受', '/round': '面试轮次', '/date': '面试日期', '/company': '公司', '/position': '岗位' } as Record<string, string>)[path] ?? '其他内容';
  const direct: Record<string, string> = { raw_text: '简历原文', additional_text: '补充内容', 'contact/name': '姓名', 'contact/email': '邮箱', 'contact/phone': '联系电话', 'contact/location': '所在城市' };
  const relative = path.startsWith('/content_json/') ? path.slice(14) : '';
  if (direct[relative]) return direct[relative];
  const simple = /^(skills|career_intent\/(?:target_roles|target_locations))\/(\d+)$/.exec(relative);
  if (simple) return `${simple[1] === 'skills' ? '技能' : simple[1].endsWith('target_roles') ? '求职意向' : '意向城市'} · 第${Number(simple[2]) + 1}项`;
  const section = /^(education|experience|projects)\/(\d+)\/([a-z_]+)(?:\/(\d+))?$/.exec(relative);
  if (!section) return '其他内容';
  const names: Record<string, string> = { education: '教育经历', experience: '工作经历', projects: '项目经历' };
  const fields: Record<string, string> = { school: '学校', degree: '学历', major: '专业', company: '公司', title: '职位', name: '名称', role: '承担角色', start_date: '开始时间', end_date: '结束时间', highlights: '经历亮点', detail: '经历描述', description: '经历描述' };
  return fields[section[3]] ? `${names[section[1]]}${Number(section[2]) + 1} · ${fields[section[3]]}${section[4] ? ` ${Number(section[4]) + 1}` : ''}` : '其他内容';
}

export interface StoryDisplayLeaf { selection: InterviewStorySourceSelection; label: string; preview: string }
export interface StorySourceGroup { key: string; kind: Kind; title: string; sections: Array<{ key: string; title: string; leaves: StoryDisplayLeaf[] }> }
export function storySourceGroups(candidates: InterviewStorySourceCandidates): StorySourceGroup[] {
  const groups: StorySourceGroup[] = [];
  for (const [kind, items] of [['resume_version', candidates.resumes], ['interview_note', candidates.interview_notes]] as const) {
    items.forEach((item, index) => groups.push({ key: `${kind}:${item.id}`, kind, title: item.label || `${STORY_SOURCE_KINDS[kind]} ${index + 1}`, sections: [{ key: 'content', title: '', leaves: item.leaves.map((leaf) => ({ ...leaf, label: storySourceLabel(kind, leaf.path), selection: { source_kind: kind, source_id: item.id, path: leaf.path } })) }] }));
  }
  const attempts = new Map<number, StorySourceGroup>();
  for (const turn of candidates.mock_turns) {
    let group = attempts.get(turn.attempt_id);
    if (!group) {
      group = { key: `mock_turn:${turn.attempt_id}`, kind: 'mock_turn', title: `模拟面试记录 ${turn.attempt_id}`, sections: [] };
      attempts.set(turn.attempt_id, group); groups.push(group);
    }
    group.sections.push({ key: String(turn.turn_no), title: `第${turn.turn_no}题`, leaves: turn.leaves.map((leaf) => ({ ...leaf, label: storySourceLabel('mock_turn', leaf.path), selection: { source_kind: 'mock_turn', source_id: turn.attempt_id, path: leaf.path } })) });
  }
  return groups;
}
export function storySelectionKey(selection: InterviewStorySourceSelection): string { return JSON.stringify([selection.source_kind, selection.source_id, selection.path]); }
