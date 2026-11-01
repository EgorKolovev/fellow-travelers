import { CarTaxiFront, CircleCheck, MessageCircle, Search } from 'lucide-react';
import { useState } from 'react';
import type { Application, Event, MyTrip, Transfer } from '../../api/types';
import {
  copyToClipboard,
  isSafeExternalUrl,
  isTelegramChatUrl,
  openExternal,
} from '../../lib/links';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { FellowList } from '../../ui/FellowList/FellowList';
import { Icon } from '../../ui/Icon/Icon';
import { SectionHeader } from '../../ui/SectionHeader/SectionHeader';
import styles from './MyTrip.module.css';
import { everyoneAtMeetingPoint, memberCaption } from './phase';

/** «Как найтись в аэропорту»: инструкция, место сбора, посадка в такси, общий чат. */
export function MeetSection({ event }: { event: Event }) {
  const meeting = event.meeting_point.address;
  const pickup = event.pickup_point.address;
  const chatUrl = event.chat_url;
  return (
    <section className={styles.section} aria-label="Как найтись в аэропорту">
      <SectionHeader title="Как найтись в аэропорту" />
      {event.meeting_instruction && (
        <p className={styles.instruction}>{event.meeting_instruction}</p>
      )}
      <p className={styles.text}>Место сбора: {meeting}</p>
      {pickup !== meeting && <p className={styles.text}>Посадка в такси: {pickup}</p>}
      {chatUrl && isTelegramChatUrl(chatUrl) && (
        <Button
          variant="secondary"
          icon={<Icon icon={MessageCircle} />}
          onClick={() => openExternal(chatUrl)}
        >
          Общий чат
        </Button>
      )}
    </section>
  );
}

const FELLOWS_TITLE = 'С кем поедете на такси';

interface FellowsProps {
  app: Application;
  transfer: Transfer | null;
  onRefresh: () => void;
  refreshing: boolean;
  onResume: () => void;
  pending: boolean;
}

/** «С кем поедете на такси» по статусу заявки. */
export function FellowsSection({
  app,
  transfer,
  onRefresh,
  refreshing,
  onResume,
  pending,
}: FellowsProps) {
  const together = app.status === 'assigned' && transfer !== null;
  // После «Уехал» действия недоступны (409 ALREADY_DEPARTED): остаётся только список попутчиков.
  if (app.departed_at && !together) return null;
  if (app.status === 'cancelled') {
    return (
      <section className={styles.card}>
        <h2 className={styles.cardTitle}>Участие отменено</h2>
        <p className={styles.text}>Вы не участвуете в подборе попутчиков</p>
        <Button onClick={onResume} disabled={pending}>
          Вернуться в поиск
        </Button>
      </section>
    );
  }
  if (app.status === 'solo') {
    return (
      <section className={styles.section}>
        <SectionHeader
          title="Вы едете один"
          description="Когда прилетите, отправляйтесь к точке посадки такси"
        />
        <Button
          variant="secondary"
          icon={<Icon icon={Search} />}
          onClick={onResume}
          disabled={pending}
        >
          Найти попутчиков
        </Button>
      </section>
    );
  }
  if (app.status === 'assigned' && transfer) {
    return (
      <section className={styles.section}>
        <SectionHeader title={FELLOWS_TITLE} />
        <FellowList
          aria-label="Попутчики"
          items={transfer.members.map((member) => {
            const caption = memberCaption(member, app);
            return {
              id: member.application_id,
              contact: member.contact,
              passengers: member.passengers,
              caption: caption.text,
              captionTone: caption.tone,
              showContacts: !member.is_me,
            };
          })}
        />
        <p className={styles.muted}>
          В машине {transfer.passengers_total} из 3 мест · багаж {transfer.baggage_total} из 2
        </p>
      </section>
    );
  }
  return (
    <section className={styles.section}>
      <SectionHeader title={FELLOWS_TITLE} />
      <EmptyState
        title="Пока никого не нашли"
        text="Подберём попутчиков, как только появятся подходящие заявки"
      />
      <Button variant="secondary" onClick={onRefresh} disabled={refreshing}>
        Обновить
      </Button>
    </section>
  );
}

interface MarksProps {
  app: Application;
  onMeetingPoint: (value: boolean) => void;
  onDeparted: () => void;
  pending: boolean;
}

/** Отметки CAP-08: «Я на месте» (обратимо) и «Я уехал» (через подтверждение). */
export function MarksSection({ app, onMeetingPoint, onDeparted, pending }: MarksProps) {
  return (
    <section className={styles.section} aria-label="Отметки">
      {app.at_meeting_point ? (
        <>
          <p className={styles.plate}>
            <Icon icon={CircleCheck} /> Вы на месте
          </p>
          <button
            type="button"
            className={styles.link}
            onClick={() => onMeetingPoint(false)}
            disabled={pending}
          >
            Я ещё не на месте
          </button>
        </>
      ) : (
        <Button
          icon={<Icon icon={CircleCheck} />}
          onClick={() => onMeetingPoint(true)}
          disabled={pending}
        >
          Я на месте
        </Button>
      )}
      <Button
        variant="dark"
        icon={<Icon icon={CarTaxiFront} />}
        onClick={onDeparted}
        disabled={pending}
      >
        Я уехал
      </Button>
    </section>
  );
}

type Copied = { what: 'from' | 'to'; ok: boolean } | null;

/** Такси CAP-10: откуда и куда с копированием, «Заказать такси», если есть ссылка. */
export function TaxiSection({ myTrip }: { myTrip: MyTrip }) {
  const [copied, setCopied] = useState<Copied>(null);
  const { taxi, transfer, application } = myTrip;
  const together = application?.status === 'assigned' && transfer !== null;
  const title =
    together && everyoneAtMeetingPoint(transfer) ? 'Пора заказывать такси' : 'Заказ такси';
  const text = together
    ? 'Договоритесь с попутчиками, кто закажет машину'
    : 'Закажите такси до места назначения';
  const url = taxi.yandex_go_url;

  async function copy(what: 'from' | 'to', address: string) {
    setCopied({ what, ok: await copyToClipboard(address) });
  }

  const row = (what: 'from' | 'to', label: string, address: string) => (
    <div className={styles.address}>
      <p className={styles.addressText}>
        {label}: {address}
      </p>
      <button
        type="button"
        className={styles.copy}
        onClick={() => void copy(what, address)}
        aria-label={`Скопировать адрес: ${label.toLowerCase()}`}
      >
        Скопировать
      </button>
    </div>
  );

  return (
    <section className={styles.section} aria-label="Такси">
      <SectionHeader title={title} description={text} />
      <div className={styles.card}>
        {row('from', 'Откуда', taxi.from.address)}
        {row('to', 'Куда', taxi.to.address)}
        <p className={styles.muted} aria-live="polite">
          {copied &&
            (copied.ok
              ? `Адрес ${copied.what === 'from' ? 'посадки' : 'назначения'} скопирован`
              : 'Не удалось скопировать — выделите адрес вручную')}
        </p>
      </div>
      {isSafeExternalUrl(url) && (
        <Button
          variant="dark"
          icon={<Icon icon={CarTaxiFront} />}
          onClick={() => openExternal(url)}
        >
          Заказать такси
        </Button>
      )}
    </section>
  );
}
