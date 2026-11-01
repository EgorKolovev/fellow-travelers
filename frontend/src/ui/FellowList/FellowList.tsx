import type { ReactNode } from 'react';
import type { Contact } from '../../api/types';
import { formatName } from '../../lib/format';
import { Avatar } from '../Avatar/Avatar';
import { ContactLinks, contactsLine } from './ContactLinks';
import styles from './FellowList.module.css';

export interface Fellow {
  /** Ключ строки (application_id или user_id). */
  id: string;
  contact: Contact;
  /** 2 — едет со спутником, рядом с именем «+1». */
  passengers?: 1 | 2;
  /** Подпись под именем («Это вы», «SU 1108 • 12:40», «На месте»). */
  caption?: ReactNode;
  /** `success` — зелёная подпись («На месте»). */
  captionTone?: 'soft' | 'success';
  /** Показывать ли контакты (для себя — не нужно). */
  showContacts?: boolean;
}

interface Props {
  items: Fellow[];
  'aria-label'?: string;
}

/** Список людей с аватаром, подписью, контактами текстом и кнопками «Написать» / «Позвонить». */
export function FellowList({ items, 'aria-label': ariaLabel }: Props) {
  return (
    <ul className={styles.list} aria-label={ariaLabel}>
      {items.map(
        ({ id, contact, passengers, caption, captionTone = 'soft', showContacts = true }) => {
          const name = formatName(contact.first_name, contact.last_name);
          const contacts = showContacts ? contactsLine(contact) : '';
          return (
            <li key={id} className={styles.row}>
              <Avatar src={contact.photo_url} size={32} decorative />
              <div className={styles.body}>
                <div className={styles.text}>
                  <p className={styles.name}>
                    {name}
                    {passengers === 2 && (
                      <>
                        <span className={styles.plus} aria-hidden="true">
                          {' '}
                          +1
                        </span>
                        <span className={styles.srOnly}>, со спутником</span>
                      </>
                    )}
                  </p>
                  {caption && (
                    <p
                      className={`${styles.caption} ${captionTone === 'success' ? styles.success : ''}`}
                    >
                      {caption}
                    </p>
                  )}
                  {contacts && <p className={styles.caption}>{contacts}</p>}
                </div>
                {showContacts && <ContactLinks contact={contact} name={name} />}
              </div>
            </li>
          );
        },
      )}
    </ul>
  );
}
