import styles from './EvidenceTechnicalDetails.module.css';

/** Keep machine identifiers available without interrupting the reading flow. */
export function EvidenceTechnicalDetails({ path }: { path: string }) {
  return <details className={styles.details}><summary>技术详情</summary><code>{path}</code></details>;
}
