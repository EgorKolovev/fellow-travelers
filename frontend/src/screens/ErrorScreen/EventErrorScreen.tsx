import { ArrowRight } from 'lucide-react';
import { useNavigate } from 'react-router';
import { isApiErrorCode, isUnauthorized } from '../../api/errors';
import { useReplace } from '../../lib/useBack';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import { ErrorScreen, OpenInTelegramScreen } from './ErrorScreen';
import styles from './ErrorScreen.module.css';

/** Не организатор на админском экране: «Управлять может только организатор» → «Моя поездка». */
export function NotAdminScreen({ eventId }: { eventId: string }) {
  const replace = useReplace();
  return (
    <Screen center>
      <EmptyState title="Управлять группой может только организатор" />
      <div className={styles.footer}>
        <Button arrow={<Icon icon={ArrowRight} />} onClick={() => replace(`/events/${eventId}`)}>
          Моя поездка
        </Button>
      </div>
    </Screen>
  );
}

export function EventNotFoundScreen() {
  const navigate = useNavigate();
  return (
    <Screen center>
      <EmptyState title="Группа не найдена" />
      <div className={styles.footer}>
        <Button onClick={() => void navigate('/')}>Мои группы</Button>
      </div>
    </Screen>
  );
}

interface Props {
  error: Error;
  eventId: string;
  onRetry: () => void;
}

/** Ошибка загрузки данных группы: 401, 403, 404 (и неверный id — 422) или прочее с повтором. */
export function EventErrorScreen({ error, eventId, onRetry }: Props) {
  if (isUnauthorized(error)) return <OpenInTelegramScreen />;
  if (isApiErrorCode(error, 'FORBIDDEN')) return <NotAdminScreen eventId={eventId} />;
  if (isApiErrorCode(error, 'NOT_FOUND') || isApiErrorCode(error, 'VALIDATION_ERROR')) {
    return <EventNotFoundScreen />;
  }
  return <ErrorScreen text={error.message} onRetry={onRetry} />;
}
