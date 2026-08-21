import type { UITurn } from '@/components/ChatPanel/model';
import { compactMessageText } from './assistantPresentation';
import styles from './AssistantSurface.module.css';

export default function CompactMessageRenderer({ turn }: { turn: UITurn }) {
  return (
    <article
      className={`${styles.message} ${turn.role === 'user' ? styles.userMessage : styles.assistantMessage}`}
      data-role={turn.role}
    >
      <span className={styles.messageAuthor}>{turn.role === 'user' ? '你' : 'Haru'}</span>
      <p>{compactMessageText(turn)}</p>
    </article>
  );
}
