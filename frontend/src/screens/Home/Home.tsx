import { ArrowRight, Luggage, Wrench } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router';
import { isUnauthorized } from '../../api/errors';
import { useMyEvents } from '../../api/hooks';
import type { EventSummary } from '../../api/types';
import { formatDateRange } from '../../lib/datetime';
import { eventStatusLabel } from '../../lib/status';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { Icon } from '../../ui/Icon/Icon';
import { Option } from '../../ui/Option/Option';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { Tabs, type Tab } from '../../ui/Tabs/Tabs';
import { ErrorScreen, OpenInTelegramScreen } from '../ErrorScreen/ErrorScreen';
import styles from './Home.module.css';

type Filter = 'all' | 'admin';

const TABS: readonly Tab<Filter>[] = [
  { id: 'all', label: 'Все' },
  { id: 'admin', label: 'Управляю' },
];

const PANEL_ID = 'my-groups-panel';

/** На «Управляю» карточка — всегда управление группой; на «Все» участник идёт в «Мою поездку». */
function asAdmin(event: EventSummary, filter: Filter): boolean {
  return filter === 'admin' || !event.is_participant;
}

/** «Мои группы» — стартовый экран, поэтому без кнопки «Назад». Вкладка — в `?filter=admin`. */
export function Home() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const filter: Filter = searchParams.get('filter') === 'admin' ? 'admin' : 'all';
  const { data: events, error, isPending, refetch } = useMyEvents();

  if (error) {
    if (isUnauthorized(error)) return <OpenInTelegramScreen />;
    return <ErrorScreen text={error.message} onRetry={() => void refetch()} />;
  }

  const footer = (
    <div className={styles.footer}>
      <Button arrow={<Icon icon={ArrowRight} />} onClick={() => void navigate('/join')}>
        Ввести код приглашения
      </Button>
      <Button variant="secondary" onClick={() => void navigate('/events/new')}>
        Создать группу
      </Button>
    </div>
  );

  if (isPending) {
    return (
      <Screen>
        <ScreenHeader title="Мои группы" />
        <div className={styles.content} aria-busy="true">
          <Skeleton height={54} />
          <Skeleton count={3} />
        </div>
        {footer}
      </Screen>
    );
  }

  if (events.length === 0) {
    return (
      <Screen>
        <ScreenHeader title="Мои группы" />
        <div className={styles.content}>
          <EmptyState
            title="Вы пока не в группах"
            text="Откройте приглашение от организатора или введите код"
          />
        </div>
        {footer}
      </Screen>
    );
  }

  const visible = filter === 'admin' ? events.filter((e) => e.is_admin) : events;
  const activeLabel = TABS.find((t) => t.id === filter)?.label;

  return (
    <Screen>
      <ScreenHeader title="Мои группы" />
      <div className={styles.content}>
        <Tabs
          tabs={TABS}
          value={filter}
          onChange={(id) =>
            setSearchParams(id === 'admin' ? { filter: 'admin' } : {}, { replace: true })
          }
          aria-label="Какие группы показать"
          panelId={() => PANEL_ID}
        />
        <div id={PANEL_ID} role="tabpanel" aria-label={activeLabel}>
          {visible.length === 0 ? (
            <EmptyState
              title="Вы пока не управляете группами"
              text="Здесь появятся группы, которые вы создали"
            />
          ) : (
            <ul className={styles.list}>
              {visible.map((event) => (
                <li key={event.id}>
                  <Option
                    leading={<Icon icon={asAdmin(event, filter) ? Wrench : Luggage} size={24} />}
                    title={event.title}
                    subtitle={`${formatDateRange(event.starts_on, event.ends_on)} · ${eventStatusLabel(event)}`}
                    onClick={() =>
                      void navigate(
                        asAdmin(event, filter)
                          ? `/events/${event.id}/admin`
                          : `/events/${event.id}`,
                      )
                    }
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      {footer}
    </Screen>
  );
}
