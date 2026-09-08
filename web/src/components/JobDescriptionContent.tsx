import styles from './JobDescriptionContent.module.css';

interface Section { title?: string; lines: string[] }

// Recognize only explicit section labels. Never summarize, deduplicate, or infer requirements.
const headingPattern = /^(?:#{1,6}\s+)?(?:\*\*)?(岗位职责|工作职责|职位描述|任职要求|岗位要求|职位要求|任职资格|加分项|优先条件|福利待遇)(?:\*\*)?\s*(?:[：:]\s*(.*))?$/;
const bulletPattern = /^\s*[-*•·]\s+|^\s*[•●]\s*/;

export default function JobDescriptionContent({ text }: { text: string }) {
  const sections: Section[] = [{ lines: [] }];
  for (const line of text.split(/\r?\n/)) {
    const headingCandidate = line.trim().replace(/^(#{1,6}\s+)?\*\*(.*?)\*\*$/, '$1$2');
    const match = headingCandidate.match(headingPattern);
    if (match) sections.push({ title: match[1], lines: match[2] ? [match[2]] : [] });
    else sections[sections.length - 1].lines.push(line);
  }
  return <div className={styles.content}>
    {sections.map((section, index) => {
      if (!section.title) return section.lines.length > 0
        ? <div key={index} className={styles.prose}>{section.lines.join('\n')}</div> : null;
      return <section key={index} className={styles.section}>
        <h5 className={styles.heading}>{section.title}</h5>
        <ul className={styles.list}>
          {section.lines.filter((line) => line.trim()).map((line, lineIndex) => (
            <li key={lineIndex}>{line.replace(bulletPattern, '')}</li>
          ))}
        </ul>
      </section>;
    })}
  </div>;
}
