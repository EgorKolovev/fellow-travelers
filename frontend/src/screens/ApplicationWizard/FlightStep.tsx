import { ArrowLeft, ArrowRight } from 'lucide-react';
import { useTrips } from '../../api/hooks';
import type { Trip } from '../../api/types';
import { formatDayMonth } from '../../lib/datetime';
import { formatFlightNumber } from '../../lib/format';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { FlightInfo } from '../../ui/FlightInfo/FlightInfo';
import { FlightOption } from '../../ui/FlightOption/FlightOption';
import { Form, FormFooter } from '../../ui/Form/Form';
import { Icon } from '../../ui/Icon/Icon';
import { Option } from '../../ui/Option/Option';
import { Screen } from '../../ui/Screen/Screen';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { TextInput } from '../../ui/TextInput/TextInput';
import { EventErrorScreen } from '../ErrorScreen/EventErrorScreen';
import styles from './ApplicationWizard.module.css';
import {
  arrivalCities,
  departureCities,
  findTrips,
  normalizeFlightNumber,
  tripsFromCity,
} from './flights';
import { WizardHeader } from './WizardHeader';

const STEP = 2;
const FROM_TITLE = 'Откуда прилетаете?';

/** Под-экраны шага «Рейс». */
export type FlightView =
  { name: 'cities' } | { name: 'city'; city: string } | { name: 'number' } | { name: 'search' };

export interface NumberQuery {
  number: string;
  /** `YYYY-MM-DD` — дата вылета в поясе аэропорта вылета. */
  date: string;
}

interface Props {
  eventId: string;
  view: FlightView;
  query: NumberQuery;
  onQueryChange: (query: NumberQuery) => void;
  /** Перейти на под-экран шага. */
  onOpen: (view: FlightView) => void;
  onPick: (trip: Trip) => void;
  onBack: () => void;
  /** Сообщение над списком (например, выбранный рейс больше недоступен). */
  notice?: string | null;
}

/** «Город → город»: если прилёт в один город — с ним, иначе только город вылета. */
function routeSubtitle(city: string, trips: Trip[]): string {
  const arrivals = arrivalCities(trips);
  return arrivals.length === 1 ? `${city} → ${arrivals[0]}` : city;
}

function TripList({ trips, onPick }: { trips: Trip[]; onPick: (trip: Trip) => void }) {
  return (
    <ul className={styles.list}>
      {trips.map((trip) => (
        <li key={trip.id}>
          <FlightOption trip={trip} withDate onClick={() => onPick(trip)} />
        </li>
      ))}
    </ul>
  );
}

/** Шаг 2 «Рейс»: выбор из рейсов группы — по городу вылета или по номеру и дате. */
export function FlightStep({
  eventId,
  view,
  query,
  onQueryChange,
  onOpen,
  onPick,
  onBack,
  notice,
}: Props) {
  const { data: trips, error, isPending, isFetching, refetch } = useTrips(eventId);

  if (isPending) {
    return (
      <Screen>
        <WizardHeader step={STEP} title={FROM_TITLE} />
        <div className={styles.content} aria-busy="true">
          <Skeleton count={4} />
        </div>
      </Screen>
    );
  }
  if (error) {
    return <EventErrorScreen error={error} eventId={eventId} onRetry={() => void refetch()} />;
  }

  const noticeLine = notice && (
    <p className={styles.notice} role="alert">
      {notice}
    </p>
  );

  if (trips.length === 0) {
    return (
      <Screen>
        <WizardHeader step={STEP} title={FROM_TITLE} />
        <div className={styles.content}>
          {noticeLine}
          <EmptyState
            title="Организатор ещё не добавил рейсы"
            text="Попробуйте позже или напишите организатору группы"
          />
        </div>
        <div className={styles.footer}>
          <Button onClick={() => void refetch()} disabled={isFetching}>
            Обновить
          </Button>
        </div>
      </Screen>
    );
  }

  switch (view.name) {
    case 'cities': {
      const arrivals = arrivalCities(trips);
      const subtitle =
        arrivals.length === 1
          ? `Выберите город, из которого летите рейсом в ${arrivals[0]}`
          : 'Выберите город, из которого летите';
      return (
        <Screen>
          <WizardHeader step={STEP} title={FROM_TITLE} subtitle={subtitle} />
          <div className={styles.content}>
            {noticeLine}
            <ul className={styles.list}>
              <li>
                <Option title="Найти по номеру рейса" onClick={() => onOpen({ name: 'number' })} />
              </li>
              {departureCities(trips).map((city) => (
                <li key={city}>
                  <Option title={city} onClick={() => onOpen({ name: 'city', city })} />
                </li>
              ))}
            </ul>
          </div>
        </Screen>
      );
    }

    case 'city': {
      const fromCity = tripsFromCity(trips, view.city);
      return (
        <Screen>
          <WizardHeader
            step={STEP}
            title="Выберите рейс"
            subtitle={routeSubtitle(view.city, fromCity)}
          />
          <div className={styles.content}>
            {fromCity.length > 0 ? (
              <TripList trips={fromCity} onPick={onPick} />
            ) : (
              <EmptyState title="Рейсов из этого города больше нет" />
            )}
          </div>
        </Screen>
      );
    }

    case 'number': {
      const ready = query.number.trim() !== '' && query.date !== '';
      return (
        <Screen>
          <WizardHeader step={STEP} title={FROM_TITLE} />
          <Form onSubmit={() => ready && onOpen({ name: 'search' })}>
            <TextInput
              label="Номер рейса"
              value={query.number}
              onChange={(number) => onQueryChange({ ...query, number })}
              placeholder="Например SU1405"
              autoCapitalize="characters"
              autoComplete="off"
              maxLength={10}
            />
            <TextInput
              label="Дата"
              type="date"
              value={query.date}
              onChange={(date) => onQueryChange({ ...query, date })}
            />
            <FormFooter>
              <Button type="submit" arrow={<Icon icon={ArrowRight} />} disabled={!ready}>
                Далее
              </Button>
            </FormFooter>
          </Form>
        </Screen>
      );
    }

    case 'search': {
      const found = findTrips(trips, query.number, query.date);
      const [first] = found;
      if (!first) {
        return (
          <Screen>
            <WizardHeader
              step={STEP}
              title="Мы не нашли рейсы"
              subtitle="Проверьте номер рейса и дату. Если рейса нет в списке — напишите организатору"
            />
            <div className={styles.footer}>
              <Button icon={<Icon icon={ArrowLeft} />} onClick={onBack}>
                Назад
              </Button>
            </div>
          </Screen>
        );
      }
      if (found.length === 1) {
        return (
          <Screen>
            <WizardHeader step={STEP} title="Это ваш рейс?" />
            <div className={styles.content}>
              <FlightInfo trip={first} />
            </div>
            <div className={styles.footer}>
              <Button onClick={() => onPick(first)}>Да</Button>
              <Button variant="secondary" onClick={onBack}>
                Нет
              </Button>
            </div>
          </Screen>
        );
      }
      const number = formatFlightNumber(normalizeFlightNumber(query.number));
      const date = formatDayMonth(first.scheduled_departure, first.departure_location.timezone);
      return (
        <Screen>
          <WizardHeader step={STEP} title="Выберите рейс" subtitle={`${number} · ${date}`} />
          <div className={styles.content}>
            <TripList trips={found} onPick={onPick} />
          </div>
        </Screen>
      );
    }
  }
}
