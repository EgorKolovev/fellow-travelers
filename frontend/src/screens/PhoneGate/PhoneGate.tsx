import { ArrowRight } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { isUnauthorized } from '../../api/errors';
import { fetchMe, meQueryKey } from '../../api/me';
import { Button } from '../../ui/Button/Button';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { Spinner } from '../../ui/Spinner/Spinner';
import { ErrorScreen, OpenInTelegramScreen } from '../ErrorScreen/ErrorScreen';
import { useShareContact } from '../Profile/useShareContact';
import styles from './PhoneGate.module.css';

const TITLE = 'Нужен номер телефона';
const TEXT =
  'Телефон нужен, чтобы попутчики могли с вами связаться. Без него нельзя вступить в группу.';

/**
 * Без телефона приложением не пользоваться (CAP-01): до любого экрана просим поделиться номером.
 * Оборачивает роутер целиком, поэтому первый экран по start param открывается уже после гейта.
 */
export function PhoneGate({ children }: { children: ReactNode }) {
  const {
    data: me,
    error,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: meQueryKey,
    queryFn: fetchMe,
  });
  const { sharing, share } = useShareContact(async () => {
    const result = await refetch();
    return result.data?.has_phone ?? false;
  });

  // Данные важнее ошибки фонового перезапроса: телефон уже есть — не выкидываем из приложения.
  if (me?.has_phone) return children;

  if (!me) {
    if (error) {
      if (isUnauthorized(error)) return <OpenInTelegramScreen />;
      return <ErrorScreen text={error.message} onRetry={() => void refetch()} />;
    }
    return (
      <Screen center>
        <Spinner />
      </Screen>
    );
  }

  const busy = sharing || isFetching;
  return (
    <Screen>
      <ScreenHeader title={TITLE} />
      <div className={styles.content}>
        <p className={styles.card}>{TEXT}</p>
      </div>
      <div className={styles.footer} aria-busy={busy || undefined}>
        <Button arrow={<Icon icon={ArrowRight} />} disabled={sharing} onClick={() => void share()}>
          Поделиться номером
        </Button>
        <Button variant="secondary" disabled={busy} onClick={() => void refetch()}>
          Проверить ещё раз
        </Button>
      </div>
    </Screen>
  );
}
