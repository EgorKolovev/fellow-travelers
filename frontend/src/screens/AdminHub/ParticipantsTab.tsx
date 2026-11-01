import { Send } from 'lucide-react';
import { useId } from 'react';
import type { ParticipantsFilters } from '../../api/admin';
import { useAdminParticipants, useAdminTransfers } from '../../api/hooks';
import type { ApplicationStatus, Counters, Trip } from '../../api/types';
import { formatDayMonth, plural } from '../../lib/datetime';
import { formatFlightNumber } from '../../lib/format';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { Icon } from '../../ui/Icon/Icon';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { sortParticipants, STATUS_FILTERS, transferNumbers } from './admin';
import styles from './AdminTabs.module.css';
import { ParticipantCard } from './ParticipantCard';
import { TabError } from './TabError';

const PEOPLE = ['человек', 'человека', 'человек'] as const;

const COUNTER_LABELS: readonly { key: Exclude<keyof Counters, 'expected'>; label: string }[] = [
  { key: 'searching', label: 'Ищут' },
  { key: 'assigned', label: 'В трансферах' },
  { key: 'solo', label: 'Едут сами' },
  { key: 'at_meeting_point', label: 'На месте' },
  { key: 'departed', label: 'Уехали' },
];

function CountersCard({ counters }: { counters: Counters }) {
  return (
    <section className={styles.card} aria-label="Сводка">
      <p className={styles.expected}>
        Ожидается {counters.expected} {plural(counters.expected, PEOPLE)}
      </p>
      <dl className={styles.counters}>
        {COUNTER_LABELS.map(({ key, label }) => (
          <div key={key} className={styles.counter}>
            <dt className={styles.counterLabel}>{label}</dt>
            <dd className={styles.counterValue}>{counters[key]}</dd>
          </div>
        ))}
      </dl>
      <p className={styles.muted}>Считаем людей вместе со спутниками; отменённые не входят</p>
    </section>
  );
}

/** Подпись рейса в фильтре: «SU 1148 · 22 августа». */
function tripLabel(trip: Trip): string {
  return `${formatFlightNumber(trip.number)} · ${formatDayMonth(trip.scheduled_departure, trip.departure_location.timezone)}`;
}

interface Props {
  eventId: string;
  filters: ParticipantsFilters;
  trips: Trip[] | undefined;
  onToggleStatus: (status: ApplicationStatus) => void;
  onTripChange: (tripId: string) => void;
  onOpenTransfer: (transferId: string) => void;
  onInvite: () => void;
}

/** Вкладка «Участники»: сводка, фильтры (уходят в API), карточки участников. */
export function ParticipantsTab({
  eventId,
  filters,
  trips,
  onToggleStatus,
  onTripChange,
  onOpenTransfer,
  onInvite,
}: Props) {
  const tripSelectId = useId();
  const { data, error, isPending, isPlaceholderData, refetch } = useAdminParticipants(
    eventId,
    filters,
  );
  const transfers = useAdminTransfers(eventId);
  const numbers = transferNumbers(transfers.data ?? []);
  const selected = new Set(filters.status ?? []);
  const filtered = selected.size > 0 || filters.trip_id !== undefined;
  // Выбранный рейс мог пропасть из списка — оставляем его в select, чтобы фильтр не потерялся.
  const unknownTrip =
    filters.trip_id !== undefined &&
    trips !== undefined &&
    !trips.some((t) => t.id === filters.trip_id);

  const filtersBlock = (
    <div className={styles.filters}>
      <fieldset className={styles.chips}>
        <legend className={styles.srOnly}>Статус заявки</legend>
        {STATUS_FILTERS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            className={`${styles.chip} ${selected.has(id) ? styles.chipActive : ''}`}
            aria-pressed={selected.has(id)}
            onClick={() => onToggleStatus(id)}
          >
            {label}
          </button>
        ))}
      </fieldset>
      <div className={styles.select}>
        <label className={styles.selectLabel} htmlFor={tripSelectId}>
          Рейс
        </label>
        <select
          id={tripSelectId}
          className={styles.selectInput}
          value={filters.trip_id ?? ''}
          onChange={(e) => onTripChange(e.target.value)}
        >
          <option value="">Все рейсы</option>
          {trips?.map((trip) => (
            <option key={trip.id} value={trip.id}>
              {tripLabel(trip)}
            </option>
          ))}
          {unknownTrip && <option value={filters.trip_id}>Выбранный рейс</option>}
        </select>
      </div>
    </div>
  );

  if (isPending) {
    return (
      <div className={styles.tab} aria-busy="true">
        <Skeleton height={140} />
        <Skeleton height={120} count={3} />
      </div>
    );
  }
  if (!data) {
    // Ошибка без данных (в т.ч. после смены фильтров) — фильтры оставляем, чтобы можно было их сменить.
    return (
      <div className={styles.tab}>
        {filtersBlock}
        <TabError error={error} onRetry={() => void refetch()} />
      </div>
    );
  }

  const participants = sortParticipants(data.participants);
  return (
    <div className={styles.tab}>
      <CountersCard counters={data.counters} />
      {filtersBlock}
      {error && (
        <p className={styles.error} role="alert">
          Не удалось обновить список: {error.message}
        </p>
      )}
      {participants.length === 0 ? (
        filtered ? (
          <EmptyState title="Никого не нашли" text="Измените фильтры" />
        ) : (
          <>
            <EmptyState title="Пока никто не вступил" text="Отправьте приглашение участникам" />
            <Button variant="secondary" icon={<Icon icon={Send} />} onClick={onInvite}>
              Приглашение
            </Button>
          </>
        )
      ) : (
        <ul
          className={styles.list}
          aria-label="Участники"
          aria-busy={isPlaceholderData || undefined}
        >
          {participants.map((p) => (
            <li key={p.contact.user_id}>
              <ParticipantCard
                participant={p}
                transferNumber={
                  p.application?.transfer_id ? numbers.get(p.application.transfer_id) : undefined
                }
                onOpenTransfer={onOpenTransfer}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
