import { useId, type HTMLAttributes } from 'react';
import fieldStyles from '../Field/Field.module.css';
import styles from './TextInput.module.css';

interface Props {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  /** Ошибка под полем (например из `details.fields` при 422). */
  error?: string;
  hint?: string;
  type?: 'text' | 'url' | 'tel' | 'date' | 'datetime-local' | 'search';
  inputMode?: HTMLAttributes<HTMLInputElement>['inputMode'];
  maxLength?: number;
  /** Границы для `type="date"` и т.п. (`YYYY-MM-DD`); у textarea игнорируются. */
  min?: string;
  max?: string;
  /** Многострочное поле (textarea). */
  multiline?: boolean;
  rows?: number;
  disabled?: boolean;
  required?: boolean;
  autoComplete?: string;
  /** Регистр экранной клавиатуры (`characters` — капс для кодов). */
  autoCapitalize?: 'off' | 'none' | 'on' | 'sentences' | 'words' | 'characters';
  name?: string;
}

/** Редактируемое поле с подписью, подсказкой и ошибкой. */
export function TextInput({
  label,
  value,
  onChange,
  placeholder,
  error,
  hint,
  type = 'text',
  inputMode,
  maxLength,
  min,
  max,
  multiline = false,
  rows = 3,
  disabled,
  required,
  autoComplete,
  autoCapitalize,
  name,
}: Props) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [error && errorId, hint && hintId].filter(Boolean).join(' ') || undefined;
  const common = {
    id,
    name,
    value,
    placeholder,
    maxLength,
    disabled,
    required,
    autoComplete,
    autoCapitalize,
    'aria-invalid': error ? true : undefined,
    'aria-describedby': describedBy,
  };
  const className = `${fieldStyles.input} ${styles.input} ${error ? styles.invalid : ''}`;

  return (
    <div className={fieldStyles.field}>
      <label className={fieldStyles.label} htmlFor={id}>
        {label}
      </label>
      {multiline ? (
        <textarea
          {...common}
          className={`${className} ${styles.textarea}`}
          rows={rows}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          {...common}
          className={className}
          type={type}
          inputMode={inputMode}
          min={min}
          max={max}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {error && (
        <p id={errorId} className={styles.error}>
          {error}
        </p>
      )}
      {hint && (
        <p id={hintId} className={fieldStyles.hint}>
          {hint}
        </p>
      )}
    </div>
  );
}
