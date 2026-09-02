import { Input } from 'antd';
import type { OfferNegotiationSnapshot } from '@/types/offer';
import styles from './OfferNegotiationPresentation.module.css';

export interface NegotiationBriefValue {
  goal: string;
  concerns: string;
  scenario: string;
}

interface Props {
  value: OfferNegotiationSnapshot['user_brief'];
  disabled: boolean;
  errors: Partial<Record<keyof NegotiationBriefValue, string>>;
  onChange: (next: NegotiationBriefValue) => void;
}

export default function NegotiationBriefForm({ value, disabled, errors, onChange }: Props) {
  const update = (field: keyof NegotiationBriefValue, next: string) => onChange({ ...value, [field]: next });
  return (
    <div className={styles.briefForm}>
      <label htmlFor="negotiation-goal">本次沟通目标</label>
      <Input
        id="negotiation-goal"
        value={value.goal}
        disabled={disabled}
        placeholder="例如：希望固定月薪再争取 2K"
        onChange={(event) => update('goal', event.target.value)}
      />
      {errors.goal && <div role="alert" className={styles.fieldError}>{errors.goal}</div>}
      <label htmlFor="negotiation-concerns">本次顾虑</label>
      <Input.TextArea
        id="negotiation-concerns"
        value={value.concerns}
        disabled={disabled}
        placeholder="例如：担心试用期、涨薪机制和加班安排"
        onChange={(event) => update('concerns', event.target.value)}
        autoSize={{ minRows: 2, maxRows: 4 }}
      />
      {errors.concerns && <div role="alert" className={styles.fieldError}>{errors.concerns}</div>}
      <label htmlFor="negotiation-scenario">沟通场景</label>
      <Input
        id="negotiation-scenario"
        value={value.scenario}
        disabled={disabled}
        placeholder="例如：HR 电话沟通、微信确认或正式谈判"
        onChange={(event) => update('scenario', event.target.value)}
      />
      {errors.scenario && <div role="alert" className={styles.fieldError}>{errors.scenario}</div>}
    </div>
  );
}
