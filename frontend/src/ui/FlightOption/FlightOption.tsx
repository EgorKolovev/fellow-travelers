import { Plane } from 'lucide-react';
import type { components } from '../../api/schema';
import { arrivalDayShift, formatDayMonth, formatTime } from '../../lib/datetime';
import { formatFlightNumber } from '../../lib/format';
import { Icon } from '../Icon/Icon';
import { Option } from '../Option/Option';
import styles from './FlightOption.module.css';

type Trip = components['schemas']['Trip'];

interface Props {
  trip: Trip;
  onClick?: () => void;
  disabled?: boolean;
  /** Добавить к номеру дату вылета («SU 1148 · 22 августа») — для списков рейсов на разные дни. */
  withDate?: boolean;
}

/** Строка маршрута «06:40 SVO → 10:40 GDZ» (+1 при прилёте на следующие сутки). */
export function flightRoute(trip: Trip): string {
  const dep = trip.departure_location;
  const arr = trip.arrival_location;
  const shift = arrivalDayShift(
    trip.scheduled_departure,
    dep.timezone,
    trip.effective_arrival,
    arr.timezone,
  );
  const route =
    `${formatTime(trip.scheduled_departure, dep.timezone)} ${dep.code} → ` +
    `${formatTime(trip.effective_arrival, arr.timezone)} ${arr.code}`;
  return shift > 0 ? `${route} +${shift}` : route;
}

/** Опция выбора рейса: номер и маршрут шрифтом Martian Mono. */
export function FlightOption({ trip, onClick, disabled, withDate = false }: Props) {
  const number = formatFlightNumber(trip.number);
  const date = formatDayMonth(trip.scheduled_departure, trip.departure_location.timezone);
  return (
    <Option
      leading={<Icon icon={Plane} size={24} />}
      title={withDate ? `${number} · ${date}` : number}
      subtitle={<span className={styles.route}>{flightRoute(trip)}</span>}
      onClick={onClick}
      disabled={disabled}
    />
  );
}
