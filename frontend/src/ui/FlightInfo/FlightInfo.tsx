import { Pencil, Plane } from 'lucide-react';
import type { components } from '../../api/schema';
import { arrivalDayShift, formatDayMonth, formatTime } from '../../lib/datetime';
import { formatBaggage, formatFlightNumber } from '../../lib/format';
import { Icon } from '../Icon/Icon';
import styles from './FlightInfo.module.css';

type Trip = components['schemas']['Trip'];

interface Props {
  trip: Trip;
  /** Число чемоданов в строке даты; не передано — не показываем. */
  baggageCount?: number;
  /** Пометка «Расписание внесено организатором» для `source=manual`. */
  showSource?: boolean;
  /** Кнопка-карандаш рядом с номером. */
  onEdit?: () => void;
  editLabel?: string;
}

type Tone = 'success' | 'error' | 'accent';

/** Сдвиг расчётного прилёта относительно планового в мс; 0, если расчётного нет. */
function arrivalDiffMs(trip: Trip): number {
  if (!trip.estimated_arrival) return 0;
  return new Date(trip.estimated_arrival).getTime() - new Date(trip.scheduled_arrival).getTime();
}

/** Рейс задержан: статус `delayed` или расчётный прилёт позже планового. */
export function isTripDelayed(trip: Trip): boolean {
  return trip.status === 'delayed' || arrivalDiffMs(trip) > 0;
}

function tripStatus(trip: Trip): { text: string; tone: Tone } | null {
  // Итоговые статусы важнее сравнения расчётного и планового времени.
  if (trip.status === 'cancelled') return { text: 'Отменён', tone: 'error' };
  if (trip.status === 'landed') return { text: 'Прилетел', tone: 'success' };
  if (isTripDelayed(trip)) return { text: 'Задержан', tone: 'error' };
  if (arrivalDiffMs(trip) < 0) return { text: 'Прилетит раньше', tone: 'success' };
  switch (trip.status) {
    case 'scheduled':
      return { text: 'По расписанию', tone: 'success' };
    case 'departed':
      return { text: 'Вылетел', tone: 'accent' };
    default:
      return null;
  }
}

/** Карточка рейса: номер, дата, время вылета и прилёта в поясах своих аэропортов, статус. */
export function FlightInfo({ trip, baggageCount, showSource = false, onEdit, editLabel }: Props) {
  const dep = trip.departure_location;
  const arr = trip.arrival_location;
  const delayed = isTripDelayed(trip);
  // Плановое время зачёркиваем, только если расчётное известно и отличается.
  const rescheduled = arrivalDiffMs(trip) !== 0;
  const shift = arrivalDayShift(
    trip.scheduled_departure,
    dep.timezone,
    trip.effective_arrival,
    arr.timezone,
  );
  const status = tripStatus(trip);
  const date = formatDayMonth(trip.scheduled_departure, dep.timezone);
  const number = formatFlightNumber(trip.number);

  return (
    <section className={styles.card} aria-label={`Рейс ${number}`}>
      <div className={styles.head}>
        <div className={styles.headText}>
          <p className={styles.number}>{number}</p>
          <p className={styles.meta}>
            {baggageCount === undefined ? date : `${date} · ${formatBaggage(baggageCount)}`}
          </p>
        </div>
        {onEdit && (
          <button
            type="button"
            className={styles.edit}
            onClick={onEdit}
            aria-label={editLabel ?? 'Изменить рейс'}
          >
            <Icon icon={Pencil} />
          </button>
        )}
      </div>

      <div className={styles.route}>
        <div className={styles.point}>
          <span className={styles.time}>
            <span className={styles.srOnly}>Вылет </span>
            {formatTime(trip.scheduled_departure, dep.timezone)}
          </span>
        </div>
        <div className={styles.line} aria-hidden="true">
          <span className={styles.rule} />
          <Icon icon={Plane} className={styles.plane} />
          <span className={styles.rule} />
        </div>
        <div className={`${styles.point} ${styles.end}`}>
          <span className={`${styles.time} ${delayed ? styles.late : ''}`}>
            <span className={styles.srOnly}>Прилёт </span>
            {formatTime(trip.effective_arrival, arr.timezone)}
            {shift > 0 && <sup className={styles.shift}>+{shift}</sup>}
          </span>
          {rescheduled && (
            <s className={styles.planned}>
              <span className={styles.srOnly}>по расписанию </span>
              {formatTime(trip.scheduled_arrival, arr.timezone)}
            </s>
          )}
        </div>
      </div>

      <div className={styles.cities}>
        <span>
          {dep.city}, {dep.code}
        </span>
        <span>
          {arr.city}, {arr.code}
        </span>
      </div>

      {status && <p className={`${styles.status} ${styles[status.tone]}`}>● {status.text}</p>}
      {showSource && trip.source === 'manual' && (
        <p className={styles.source}>Расписание внесено организатором</p>
      )}
    </section>
  );
}
