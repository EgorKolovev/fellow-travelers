import { ArrowRight, MessageCircle } from 'lucide-react';
import { useParams } from 'react-router';
import { useAdminTransfers, useEvent } from '../../api/hooks';
import type { Event, Transfer } from '../../api/types';
import { isTelegramChatUrl, openExternal } from '../../lib/links';
import { useBack, useReplace } from '../../lib/useBack';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { FellowList } from '../../ui/FellowList/FellowList';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { SectionHeader } from '../../ui/SectionHeader/SectionHeader';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import {
  adminMemberCaption,
  transferNumbers,
  transferState,
  type TransferState,
} from '../AdminHub/admin';
import { EventErrorScreen, NotAdminScreen } from '../ErrorScreen/EventErrorScreen';
import styles from './TransferDetails.module.css';

const STATE: Record<TransferState, { title: string; text: string; tone: string | undefined }> = {
  departed: { title: 'Уехали', text: 'Все участники отметили «Уехал»', tone: styles.soft },
  ready: {
    title: 'Готов к заказу такси',
    text: 'Все участники пришли к месту сбора',
    tone: styles.success,
  },
  gathering: {
    title: 'Собираются',
    text: 'Ждём, пока все придут к месту сбора',
    tone: styles.accent,
  },
};

function TransferBody({ event, transfer }: { event: Event; transfer: Transfer }) {
  const state = STATE[transferState(transfer)];
  const chatUrl = event.chat_url;
  return (
    <div className={styles.content}>
      <section className={styles.card} aria-label="Статус трансфера">
        <p className={`${styles.state} ${state.tone ?? ''}`}>
          <span aria-hidden="true">● </span>
          {state.title}
        </p>
        <p className={styles.muted}>{state.text}</p>
      </section>
      <section className={styles.section}>
        <SectionHeader title={`Кто едет · ${transfer.passengers_total}`} />
        <FellowList
          aria-label="Кто едет"
          items={transfer.members.map((member) => {
            const caption = adminMemberCaption(member);
            return {
              id: member.application_id,
              contact: member.contact,
              passengers: member.passengers,
              caption: (
                <>
                  {caption.text}
                  {caption.mark && (
                    <>
                      {' · '}
                      <span className={caption.mark.tone === 'success' ? styles.markSuccess : ''}>
                        {caption.mark.text}
                      </span>
                    </>
                  )}
                </>
              ),
              showContacts: !member.is_me,
            };
          })}
        />
        <p className={styles.muted}>Место сбора: {event.meeting_point.address}</p>
        {chatUrl && isTelegramChatUrl(chatUrl) && (
          <Button
            variant="secondary"
            icon={<Icon icon={MessageCircle} />}
            onClick={() => openExternal(chatUrl)}
          >
            Открыть общий чат
          </Button>
        )}
      </section>
    </div>
  );
}

/** Состав трансфера для организатора `/events/:id/admin/transfers/:tid`. */
export function TransferDetails() {
  const params = useParams();
  const eventId = params.id ?? '';
  const transferId = params.tid ?? '';
  const listPath = `/events/${eventId}/admin?tab=transfers`;
  const back = useBack(listPath);
  const replace = useReplace();
  const event = useEvent(eventId);
  const isAdmin = event.data?.is_admin === true;
  // Список трансферов — из кэша вкладки или запросом; не админу не запрашиваем.
  const transfers = useAdminTransfers(eventId, { enabled: isAdmin });

  if (event.isPending || (isAdmin && transfers.isPending)) {
    return (
      <Screen>
        <div className={styles.content} aria-busy="true">
          <Skeleton height={80} />
          <Skeleton height={68} count={3} />
        </div>
      </Screen>
    );
  }
  if (event.error) {
    return (
      <EventErrorScreen
        error={event.error}
        eventId={eventId}
        onRetry={() => void event.refetch()}
      />
    );
  }
  if (!event.data.is_admin) return <NotAdminScreen eventId={eventId} />;
  if (transfers.error) {
    return (
      <EventErrorScreen
        error={transfers.error}
        eventId={eventId}
        onRetry={() => void transfers.refetch()}
      />
    );
  }

  const list = transfers.data ?? [];
  const transfer = list.find((t) => t.id === transferId);
  const number = transfer ? transferNumbers(list).get(transfer.id) : undefined;
  return (
    <Screen>
      <ScreenHeader
        eyebrow="Все трансферы"
        onEyebrowClick={back}
        title={number ? `Трансфер ${number}` : 'Трансфер'}
      />
      {transfer ? (
        <TransferBody event={event.data} transfer={transfer} />
      ) : (
        <div className={styles.content}>
          <EmptyState
            title="Трансфер не найден"
            text="Возможно, его расформировали — посмотрите актуальный список"
          />
          <Button arrow={<Icon icon={ArrowRight} />} onClick={() => replace(listPath)}>
            Все трансферы
          </Button>
        </div>
      )}
    </Screen>
  );
}
