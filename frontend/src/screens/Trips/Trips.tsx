import { ArrowRight } from 'lucide-react';
import { useNavigate, useParams } from 'react-router';
import { useEvent, useTrips } from '../../api/hooks';
import type { Trip } from '../../api/types';
import { useBack } from '../../lib/useBack';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { FlightOption } from '../../ui/FlightOption/FlightOption';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { EventErrorScreen, NotAdminScreen } from '../ErrorScreen/EventErrorScreen';
import styles from './Trips.module.css';

function byDeparture(a: Trip, b: Trip): number {
  return Date.parse(a.scheduled_departure) - Date.parse(b.scheduled_departure);
}

/** Рейсы группы `/events/:id/admin/trips`: список по времени вылета, добавление и правка. */
export function Trips() {
  const eventId = useParams().id ?? '';
  const navigate = useNavigate();
  useBack(`/events/${eventId}/admin`);
  const event = useEvent(eventId);
  const trips = useTrips(eventId);
  const base = `/events/${eventId}/admin/trips`;

  const header = (
    <ScreenHeader
      eyebrow="Администратор"
      title="Рейсы группы"
      subtitle="Участники выбирают рейс из этого списка"
    />
  );
  const footer = (
    <div className={styles.footer}>
      <Button arrow={<Icon icon={ArrowRight} />} onClick={() => void navigate(`${base}/new`)}>
        Добавить рейс
      </Button>
    </div>
  );

  if (event.isPending || trips.isPending) {
    return (
      <Screen>
        {header}
        <div className={styles.content} aria-busy="true">
          <Skeleton count={3} />
        </div>
      </Screen>
    );
  }
  const error = event.error ?? trips.error;
  if (error) {
    const retry = () => void (event.error ? event.refetch() : trips.refetch());
    return <EventErrorScreen error={error} eventId={eventId} onRetry={retry} />;
  }
  if (!event.data?.is_admin) return <NotAdminScreen eventId={eventId} />;

  const sorted = [...(trips.data ?? [])].sort(byDeparture);

  return (
    <Screen>
      {header}
      <div className={styles.content}>
        {sorted.length === 0 ? (
          <EmptyState title="Рейсов пока нет" text="Добавьте рейсы, которыми летят участники" />
        ) : (
          <ul className={styles.list}>
            {sorted.map((trip) => (
              <li key={trip.id}>
                <FlightOption
                  trip={trip}
                  withDate
                  onClick={() => void navigate(`${base}/${trip.id}`)}
                />
              </li>
            ))}
          </ul>
        )}
      </div>
      {footer}
    </Screen>
  );
}
