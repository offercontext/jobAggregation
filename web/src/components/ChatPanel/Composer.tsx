import { useEffect, useMemo, useRef, useState } from 'react';
import { Input, Button } from 'antd';
import { SendOutlined } from '@ant-design/icons';
import type { Capability } from './capabilities';
import SlashMenu from './SlashMenu';
import styles from './ChatPanel.module.css';
import type { SendMessageOutcome } from '@/features/assistantSurface/usePilotConversationController';

interface Props {
  capabilities: Capability[];
  disabled?: boolean;
  disabledReason?: string;
  placeholder?: string;
  resetKey?: number;
  draftValue?: string;
  onDraftChange?: (value: string) => void;
  suggestions?: string[];
  onSuggestionSelect?: (question: string) => void;
  onboardingFocusToken?: number;
  onSend: (text: string) => void | boolean | SendMessageOutcome | Promise<void | boolean | SendMessageOutcome>;
}

function shouldClearAfterSend(result: void | boolean | SendMessageOutcome): boolean {
  return result !== false && result !== 'failed' && result !== 'stopped' && result !== 'ignored';
}

export default function Composer({
  capabilities,
  disabled,
  disabledReason,
  placeholder,
  resetKey,
  draftValue,
  onDraftChange,
  suggestions,
  onSuggestionSelect,
  onboardingFocusToken,
  onSend,
}: Props) {
  const [localValue, setLocalValue] = useState('');
  const [sel, setSel] = useState(0);
  const [onboardingFocusActive, setOnboardingFocusActive] = useState(false);
  const composerRef = useRef<HTMLDivElement>(null);
  const controlled = draftValue !== undefined;
  const value = controlled ? draftValue : localValue;
  const setValue = (next: string) => {
    if (controlled) onDraftChange?.(next);
    else setLocalValue(next);
  };

  useEffect(() => {
    if (!controlled) setLocalValue('');
    setSel(0);
  }, [controlled, resetKey]);

  useEffect(() => {
    if (!onboardingFocusToken) {
      setOnboardingFocusActive(false);
      return;
    }

    setOnboardingFocusActive(true);
    const focusTimer = window.setTimeout(() => {
      composerRef.current?.querySelector('textarea')?.focus();
    }, 0);
    const resetTimer = window.setTimeout(() => setOnboardingFocusActive(false), 2400);
    return () => {
      window.clearTimeout(focusTimer);
      window.clearTimeout(resetTimer);
    };
  }, [onboardingFocusToken]);

  const slashQuery = value.startsWith('/') ? value.slice(1).trim().toLowerCase() : null;
  const menuOpen = slashQuery !== null && !disabled;
  const quickQuestions = !disabled && !value ? (suggestions ?? []).slice(0, 3) : [];

  const items = useMemo(() => {
    if (slashQuery === null) return [];
    if (!slashQuery) return capabilities;
    return capabilities.filter(
      (c) => c.label.toLowerCase().includes(slashQuery) || c.hint.toLowerCase().includes(slashQuery),
    );
  }, [slashQuery, capabilities]);

  async function pickCapability(cap: Capability) {
    const needsMore = /[：:]$/.test(cap.prompt.trim());
    if (needsMore) {
      setValue(cap.prompt);
      setSel(0);
      return;
    }

    const sent = await onSend(cap.prompt);
    if (shouldClearAfterSend(sent)) {
      setValue('');
      setSel(0);
    }
  }

  async function submit() {
    const text = value.trim();
    if (!text || disabled) return;
    const sent = await onSend(text);
    if (shouldClearAfterSend(sent)) {
      setValue('');
      setSel(0);
    }
  }

  return (
    <div
      ref={composerRef}
      className={`${styles.composer} ${onboardingFocusActive ? styles.composerOnboardingFocus : ''}`}
    >
      {quickQuestions.length > 0 ? (
        <div className={styles.quickQuestions} aria-label="快捷提问">
          {quickQuestions.map((question, index) => (
            <button
              key={question}
              type="button"
              className={styles.quickQuestion}
              data-testid={`quick-question-${index}`}
              onClick={() => {
                setValue(question);
                setSel(0);
                onSuggestionSelect?.(question);
              }}
            >
              {question}
            </button>
          ))}
        </div>
      ) : null}
      {menuOpen && items.length > 0 && (
        <SlashMenu items={items} selected={sel} onSelect={pickCapability} onHover={setSel} />
      )}
      <div className={styles.composerBox}>
        <Input.TextArea
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setSel(0);
          }}
          onKeyDown={(e) => {
            if (menuOpen && items.length > 0) {
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                setSel((s) => (s + 1) % items.length);
                return;
              }
              if (e.key === 'ArrowUp') {
                e.preventDefault();
                setSel((s) => (s - 1 + items.length) % items.length);
                return;
              }
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void pickCapability(items[sel]);
                return;
              }
              if (e.key === 'Escape') {
                e.preventDefault();
                setValue('');
                return;
              }
            }
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
          placeholder={placeholder ?? '问问领航员，或输入 / 唤起能力'}
          autoSize={{ minRows: 1, maxRows: 4 }}
          variant="borderless"
          disabled={disabled}
        />
        <Button
          type="primary"
          className="op-ai-btn"
          icon={<SendOutlined />}
          disabled={disabled || !value.trim()}
          onClick={() => void submit()}
          aria-label="发送"
        />
      </div>
      <div className={styles.hint}>
        {disabledReason ? (
          <span>{disabledReason}</span>
        ) : (
          <span>
            输入 <kbd>/</kbd> 唤起能力 · <kbd>Enter</kbd> 发送 · <kbd>Shift+Enter</kbd> 换行
          </span>
        )}
      </div>
    </div>
  );
}
