// Interview retrospective note — mirrors Go db.InterviewNote JSON tags.
export interface InterviewNote {
  id: number;
  application_id?: number;
  application_event_id?: number | null;
  company: string;
  position: string;
  round: string;
  date: string;
  questions: string;
  self_reflection: string;
  difficulty_points: string;
  mood: string;
  revision: number;
  created_at: string;
}

export interface CreateNoteInput {
  application_id?: number;
  application_event_id?: number | null;
  company?: string;
  position?: string;
  round?: string;
  date?: string;
  questions?: string;
  self_reflection?: string;
  difficulty_points?: string;
  mood?: string;
}

export type UpdateNoteInput = CreateNoteInput;
