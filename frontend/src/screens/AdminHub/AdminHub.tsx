import { Luggage, Plane, Send, Settings } from 'lucide-react';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { isUnauthorized } from '../../api/errors';
import { useEvent, useJoinAsParticipant, useTrips } from '../../api/hooks';
import { formatDateRange, plural } from '../../lib/datetime';
import { useBack, useReplaceSearch } from '../../lib/useBack';
import { Button } from '../../ui/Button/Button';
import { Icon } from '../../ui/Icon/Icon';
import { Option } from '../../ui/Option/Option';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { Tabs, type Tab } from '../../ui/Tabs/Tabs';
import { EventErrorScreen, NotAdminScreen } from '../ErrorScreen/EventErrorScreen';
import { OpenInTelegramScreen } from '../ErrorScreen/ErrorScreen';
import { readFilters, readTab, setTab, setTrip, toggleStatus, type AdminTab } from './admin';
import styles from './AdminHub.module.css';
import { ParticipantsTab } from './ParticipantsTab';
import { TransfersTab } from './TransfersTab';

const TRIPS_HINT = 'Участники выбирают рейс из этого списка';

const TABS: readonly Tab<AdminTab>[] = [
  { id: 'participants', label: 'Участники' },
  { id: 'transfers', label: 'Трансферы' },
];

const panelId = (tab: AdminTab) => `admin-panel-${tab}`;

/**
 * Управление группой `/events/:id/admin`: приглашение, рейсы, настройки и вкладки
 * «Участники» / «Трансферы». Вкладка и фильтры — в query-параметрах (`?tab=transfers&status=…&trip=…`),
 * чтобы переживать возврат назад.
 */
export function AdminHub() {
  const eventId = useParams().id ?? '';
  const navigate = useNavigate();
  useBack('/');
  const [searchParams] = useSearchParams();
  const replaceSearch = useReplaceSearch();
  const tab = readTab(searchParams);
  const filters = readFilters(searchParams);
  const { data: event, error, isPending, refetch } = useEvent(eventId);
  const isAdmin = event?.is_admin === true;
  const trips = useTrips(eventId);
  // Код нужен только админу без участия — чтобы вступить по нему самому.
  const join = useJoinAsParticipant(eventId, { enabled: isAdmin && !event?.is_participant });

  if (isPending) {
    return (
      <Screen>
        <div className={styles.content} aria-busy="true">
          <Skeleton height={80} />
          <Skeleton count={3} />
        </div>
      </Screen>
    );
  }
  if (error) {
    return <EventErrorScreen error={error} eventId={eventId} onRetry={() => void refetch()} />;
  }
  if (!event.is_admin) return <NotAdminScreen eventId={eventId} />;
  if (join.error && isUnauthorized(join.error)) return <OpenInTelegramScreen />;

  const tripsCount = trips.data?.length;
  const tripsSubtitle =
    tripsCount === undefined
      ? TRIPS_HINT
      : `${tripsCount} ${plural(tripsCount, ['рейс', 'рейса', 'рейсов'])} · ${TRIPS_HINT}`;
  const go = (path: string) => () => void navigate(`/events/${eventId}${path}`);
  const openTransfer = (transferId: string) => go(`/admin/transfers/${transferId}`)();

  function joinAsParticipant() {
    join.join(() => void navigate(`/events/${eventId}`));
  }

  return (
    <Screen>
      <ScreenHeader
        eyebrow="Администратор"
        title={event.title}
        subtitle={formatDateRange(event.starts_on, event.ends_on)}
      />
      <div className={styles.content}>
        {!event.join_open && (
          <p className={styles.notice}>
            Вступление закрыто — новые участники не смогут присоединиться
          </p>
        )}
        <ul className={styles.list}>
          {event.is_participant && (
            <li>
              <Option
                leading={<Icon icon={Luggage} size={24} />}
                title="Моя поездка"
                subtitle="Ваша заявка и попутчики"
                onClick={go('')}
              />
            </li>
          )}
          <li>
            <Option
              leading={<Icon icon={Send} size={24} />}
              title="Приглашение"
              subtitle="Код и ссылка для участников"
              onClick={go('/admin/invite')}
            />
          </li>
          <li>
            <Option
              leading={<Icon icon={Plane} size={24} />}
              title="Рейсы группы"
              subtitle={tripsSubtitle}
              onClick={go('/admin/trips')}
            />
          </li>
          <li>
            <Option
              leading={<Icon icon={Settings} size={24} />}
              title="Настройки группы"
              subtitle="Точки, инструкция, вступление"
              onClick={go('/admin/settings')}
            />
          </li>
        </ul>
        {!event.is_participant && (
          <section className={styles.join} aria-labelledby="admin-join-title">
            <p id="admin-join-title" className={styles.joinText}>
              Едете сами? Вступите как участник, чтобы подать заявку
            </p>
            <Button
              variant="secondary"
              disabled={!join.canJoin}
              aria-busy={join.isPending || undefined}
              onClick={joinAsParticipant}
            >
              Вступить как участник
            </Button>
            {join.error && (
              <p className={styles.error} role="alert">
                {join.error.message}
              </p>
            )}
          </section>
        )}
        <div className={styles.tabs}>
          <Tabs
            tabs={TABS}
            value={tab}
            onChange={(next) => replaceSearch(setTab(searchParams, next))}
            aria-label="Участники и трансферы"
            panelId={panelId}
          />
          <div role="tabpanel" id={panelId(tab)} aria-label={TABS.find((t) => t.id === tab)?.label}>
            {tab === 'participants' && (
              <ParticipantsTab
                eventId={eventId}
                filters={filters}
                trips={trips.data}
                onToggleStatus={(status) => replaceSearch(toggleStatus(searchParams, status))}
                onTripChange={(tripId) => replaceSearch(setTrip(searchParams, tripId))}
                onOpenTransfer={openTransfer}
                onInvite={go('/admin/invite')}
              />
            )}
            {tab === 'transfers' && (
              <TransfersTab eventId={eventId} onOpenTransfer={openTransfer} />
            )}
          </div>
        </div>
      </div>
    </Screen>
  );
}
