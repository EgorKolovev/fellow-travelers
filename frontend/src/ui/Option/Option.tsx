import { ChevronRight, Pencil } from 'lucide-react';
import type { ReactNode } from 'react';
import { Icon } from '../Icon/Icon';
import styles from './Option.module.css';

interface Props {
  title: ReactNode;
  subtitle?: ReactNode;
  /**
   * Слева: иконка (`<Icon icon={Plane} size={24} />`) или элемент (`<Avatar size={32} decorative />`).
   * Слот декоративный — скрыт от скринридера, смысл должен быть в заголовке.
   */
  leading?: ReactNode;
  /** Справа: шеврон (выбор), карандаш (правка) или ничего. По умолчанию шеврон у кнопки, ничего у блока. */
  trailing?: 'arrow' | 'edit' | 'none';
  /** С обработчиком карточка — кнопка, без него — просто блок. */
  onClick?: () => void;
  disabled?: boolean;
  /** Имя кнопки для скринридера, если текста карточки недостаточно (например «Изменить рейс»). */
  'aria-label'?: string;
}

const TRAILING = {
  arrow: <Icon icon={ChevronRight} />,
  edit: <Icon icon={Pencil} />,
  none: null,
} as const;

/** Белая карточка-выбор 68px. */
export function Option({
  title,
  subtitle,
  leading,
  trailing,
  onClick,
  disabled = false,
  'aria-label': ariaLabel,
}: Props) {
  const trailingIcon = TRAILING[trailing ?? (onClick ? 'arrow' : 'none')];
  const content = (
    <>
      {leading !== undefined && (
        <span className={styles.leading} aria-hidden="true">
          {leading}
        </span>
      )}
      <span className={styles.text}>
        <span className={styles.title}>{title}</span>
        {subtitle && <span className={styles.subtitle}>{subtitle}</span>}
      </span>
      {trailingIcon && (
        <span className={styles.trailing} aria-hidden="true">
          {trailingIcon}
        </span>
      )}
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        className={`${styles.option} ${styles.clickable}`}
        onClick={onClick}
        disabled={disabled}
        aria-label={ariaLabel}
      >
        {content}
      </button>
    );
  }
  return (
    <div
      className={`${styles.option} ${disabled ? styles.disabled : ''}`}
      aria-disabled={disabled || undefined}
    >
      {content}
    </div>
  );
}
