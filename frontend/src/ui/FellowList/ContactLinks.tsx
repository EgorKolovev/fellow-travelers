import { MessageCircle, Phone } from 'lucide-react';
import type { MouseEvent } from 'react';
import type { Contact } from '../../api/types';
import { formatPhone } from '../../lib/format';
import { openExternal, telegramUserUrl, telUrl } from '../../lib/links';
import { Icon } from '../Icon/Icon';
import styles from './FellowList.module.css';

function openTelegram(e: MouseEvent<HTMLAnchorElement>) {
  // Внутри Telegram ссылку t.me открываем через SDK; вне его сработает обычный переход.
  if (openExternal(e.currentTarget.href)) e.preventDefault();
}

/** Контакты текстом: «+7 912 204 29 12 · @username»; пустая строка, если контактов нет. */
export function contactsLine(contact: Contact): string {
  const parts: string[] = [];
  if (contact.phone) parts.push(formatPhone(contact.phone));
  if (contact.username) parts.push(`@${contact.username}`);
  return parts.join(' · ');
}

interface Props {
  contact: Contact;
  /** Имя для подписей ссылок («Написать в Telegram: …»). */
  name: string;
}

/** Круглые кнопки «Написать» (t.me) и «Позвонить» (`tel:`) — только для валидных username и телефона. */
export function ContactLinks({ contact, name }: Props) {
  const tgUrl = telegramUserUrl(contact.username);
  const phoneUrl = telUrl(contact.phone);
  if (!tgUrl && !phoneUrl) return null;
  return (
    <div className={styles.actions}>
      {tgUrl && (
        <a
          className={styles.action}
          href={tgUrl}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Написать в Telegram: ${name}`}
          onClick={openTelegram}
        >
          <Icon icon={MessageCircle} size={18} />
        </a>
      )}
      {phoneUrl && (
        <a className={styles.action} href={phoneUrl} aria-label={`Позвонить: ${name}`}>
          <Icon icon={Phone} size={18} />
        </a>
      )}
    </div>
  );
}
