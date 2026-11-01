import { SearchX } from 'lucide-react';
import type { ReactNode } from 'react';
import { Icon } from '../Icon/Icon';
import styles from './EmptyState.module.css';

interface Props {
  title: string;
  text?: ReactNode;
  /** Крупная иконка над заголовком; по умолчанию «ничего не нашли». */
  icon?: ReactNode;
}

/** Белая карточка «пусто / не получилось»: иконка, заголовок акцентным цветом, текст. */
export function EmptyState({
  title,
  text,
  icon = <Icon icon={SearchX} size={40} strokeWidth={1.5} />,
}: Props) {
  return (
    <section className={styles.card}>
      <span className={styles.icon} aria-hidden="true">
        {icon}
      </span>
      <h2 className={styles.title}>{title}</h2>
      {text && <p className={styles.text}>{text}</p>}
    </section>
  );
}
