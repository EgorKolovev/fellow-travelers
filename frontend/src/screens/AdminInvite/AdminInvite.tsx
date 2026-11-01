import { ArrowRight } from 'lucide-react';
import { useState } from 'react';
import { useParams } from 'react-router';
import { useEvent, useInvite } from '../../api/hooks';
import { copyToClipboard, openExternal, shareUrl } from '../../lib/links';
import { useBack } from '../../lib/useBack';
import { Button } from '../../ui/Button/Button';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { EventErrorScreen, NotAdminScreen } from '../ErrorScreen/EventErrorScreen';
import styles from './AdminInvite.module.css';

/** Текст для «Поделиться». */
export function inviteShareText(title: string): string {
  return `Присоединяйтесь к группе «${title}» — подберём попутчиков из аэропорта`;
}

/** Приглашение `/events/:id/admin/invite`: код, ссылка, «Поделиться» и копирование. */
export function AdminInvite() {
  const eventId = useParams().id ?? '';
  useBack(`/events/${eventId}/admin`);
  const event = useEvent(eventId);
  const invite = useInvite(eventId, { enabled: event.data?.is_admin === true });
  // Отклик о копировании — для всех через aria-live.
  const [status, setStatus] = useState<{ text: string; ok: boolean } | null>(null);

  if (event.isPending || (event.data?.is_admin && invite.isPending)) {
    return (
      <Screen>
        <div aria-busy="true" className={styles.loading}>
          <Skeleton height={80} />
          <Skeleton height={120} />
        </div>
      </Screen>
    );
  }
  const error = event.error ?? invite.error;
  if (error) {
    const retry = () => void (event.error ? event.refetch() : invite.refetch());
    return <EventErrorScreen error={error} eventId={eventId} onRetry={retry} />;
  }
  const group = event.data;
  // Без ошибки и загрузки данные группы есть; без кода — значит, не админ.
  if (!group?.is_admin || !invite.data) return <NotAdminScreen eventId={eventId} />;

  const { code, link } = invite.data;

  async function copy(text: string, done: string) {
    const ok = await copyToClipboard(text);
    setStatus({ ok, text: ok ? done : 'Не удалось скопировать' });
  }

  return (
    <Screen>
      <ScreenHeader
        eyebrow="Администратор"
        title="Приглашение"
        subtitle="Отправьте ссылку участникам или продиктуйте код"
      />
      <div className={styles.content}>
        {!group.join_open && (
          <p className={styles.warning} role="note">
            Вступление закрыто — по этой ссылке сейчас нельзя присоединиться
          </p>
        )}
        <section className={styles.card} aria-label="Код приглашения">
          <p className={styles.code}>{code}</p>
          <p className={styles.link}>{link}</p>
        </section>
      </div>
      <div className={styles.footer}>
        <Button
          arrow={<Icon icon={ArrowRight} />}
          onClick={() => openExternal(shareUrl(link, inviteShareText(group.title)))}
        >
          Поделиться
        </Button>
        <Button variant="secondary" onClick={() => void copy(link, 'Ссылка скопирована')}>
          Скопировать ссылку
        </Button>
        <Button variant="secondary" onClick={() => void copy(code, 'Код скопирован')}>
          Скопировать код
        </Button>
        <p
          className={`${styles.status} ${status && !status.ok ? styles.failed : ''}`}
          aria-live="polite"
        >
          {status?.text}
        </p>
      </div>
    </Screen>
  );
}
