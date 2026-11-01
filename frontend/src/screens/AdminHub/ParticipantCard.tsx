import { ChevronRight } from 'lucide-react';
import type { AdminParticipant } from '../../api/types';
import { formatTime } from '../../lib/datetime';
import { formatBaggage, formatFlightNumber, formatName, formatWait } from '../../lib/format';
import { APPLICATION_STATUS_LABEL } from '../../lib/status';
import { Avatar } from '../../ui/Avatar/Avatar';
import { ContactLinks, contactsLine } from '../../ui/FellowList/ContactLinks';
import { Icon } from '../../ui/Icon/Icon';
import styles from './AdminTabs.module.css';

interface Props {
  participant: AdminParticipant;
  /** Номер назначенного трансфера (`transferNumbers`); нет — трансферы ещё не загружены. */
  transferNumber?: number;
  onOpenTransfer: (transferId: string) => void;
}

/** Карточка участника для организатора: контакты, рейс, багаж, ожидание, статус, отметки, трансфер. */
export function ParticipantCard({ participant, transferNumber, onOpenTransfer }: Props) {
  const { contact, application: app } = participant;
  const name = formatName(contact.first_name, contact.last_name);
  const contacts = contactsLine(contact);
  return (
    <article className={styles.participant} aria-label={name}>
      <div className={styles.participantHead}>
        <Avatar src={contact.photo_url} size={32} decorative />
        <div className={styles.participantText}>
          <p className={styles.name}>
            {name}
            {app?.passengers === 2 && (
              <>
                <span className={styles.plus} aria-hidden="true">
                  {' '}
                  +1
                </span>
                <span className={styles.srOnly}>, со спутником</span>
              </>
            )}
          </p>
          {contacts && <p className={styles.caption}>{contacts}</p>}
        </div>
        <ContactLinks contact={contact} name={name} />
      </div>
      {app ? (
        <div className={styles.participantBody}>
          <p className={styles.line}>
            {formatFlightNumber(app.trip.number)} · прилёт{' '}
            {formatTime(app.trip.effective_arrival, app.trip.arrival_location.timezone)}
          </p>
          <p className={styles.caption}>
            Багаж: {formatBaggage(app.baggage_count)} ·{' '}
            {app.max_wait_minutes > 0 ? `Ждёт: ${formatWait(app.max_wait_minutes)}` : 'Не ждёт'}
          </p>
          <p className={styles.statusLine}>
            <span>{APPLICATION_STATUS_LABEL[app.status]}</span>
            {app.departed_at ? (
              <span className={styles.mark}>Уехал</span>
            ) : (
              app.at_meeting_point && (
                <span className={`${styles.mark} ${styles.success}`}>На месте</span>
              )
            )}
          </p>
          {app.transfer_id && (
            <button
              type="button"
              className={styles.linkButton}
              onClick={() => app.transfer_id && onOpenTransfer(app.transfer_id)}
            >
              {transferNumber ? `Трансфер ${transferNumber}` : 'Трансфер'}
              <Icon icon={ChevronRight} size={16} />
            </button>
          )}
        </div>
      ) : (
        <p className={`${styles.caption} ${styles.pale}`}>Заявка не заполнена</p>
      )}
    </article>
  );
}
