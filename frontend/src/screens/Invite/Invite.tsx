import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router';
import { isApiErrorCode, isUnauthorized } from '../../api/errors';
import { useInvitePreview, useJoin } from '../../api/hooks';
import { fetchMe, meQueryKey } from '../../api/me';
import { formatDateRange } from '../../lib/datetime';
import { isInviteCode } from '../../lib/inviteCode';
import { useBack, useIsFirstEntry, useReplace } from '../../lib/useBack';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { ErrorScreen, OpenInTelegramScreen } from '../ErrorScreen/ErrorScreen';
import { useShareContact } from '../Profile/useShareContact';
import styles from './Invite.module.css';

const EYEBROW = 'Приглашение';

const HINT =
  'После вступления заполните заявку: рейс, багаж и сколько готовы ждать. ' +
  'Мы подберём попутчиков до места встречи.';

const PHONE_REQUIRED_TEXT =
  'Чтобы вступить в группу, поделитесь номером телефона — по нему с вами свяжутся попутчики.';

/** Код не найден (или в неверном формате — тоже «не нашли»). */
function isNotFound(error: unknown): boolean {
  return isApiErrorCode(error, 'INVITE_NOT_FOUND') || isApiErrorCode(error, 'VALIDATION_ERROR');
}

/** Превью приглашения `/invite/:code` → «Вступить» → `/events/:id`. */
export function Invite() {
  const code = (useParams().code ?? '').toUpperCase();
  const navigate = useNavigate();
  const replace = useReplace();
  const first = useIsFirstEntry();
  useBack('/');
  // Неверный формат кода — «не нашли» сразу, без запроса.
  const validCode = isInviteCode(code);
  const {
    data: preview,
    error,
    isPending,
    refetch,
  } = useInvitePreview(code, {
    enabled: validCode,
  });
  const join = useJoin();
  const { mutate: mutateJoin, submittedAt } = join;
  /** Вступил: группа становится «первым» экраном — её «Назад» ведёт на «Мои группы», а не на ввод кода. */
  const onJoin = useCallback(() => {
    mutateJoin(code, {
      onSuccess: (event) => replace(`/events/${event.id}`, { asFirst: true }),
    });
  }, [mutateJoin, code, replace]);
  const alreadyMember = preview?.already_member === true;
  const phoneRequired = isApiErrorCode(join.error, 'PHONE_REQUIRED');

  // Профиль только читаем из кэша (его грузит PhoneGate): свой запрос не нужен.
  const client = useQueryClient();
  const { data: me, dataUpdatedAt: meUpdatedAt } = useQuery({
    queryKey: meQueryKey,
    queryFn: fetchMe,
    enabled: false,
  });
  // Опрашиваем /me напрямую и кладём в кэш только «телефон есть»: иначе PhoneGate закрыл бы экран,
  // и вступление после появления телефона не повторилось бы.
  const { sharing, share } = useShareContact(async () => {
    try {
      const fresh = await fetchMe();
      if (fresh.has_phone) client.setQueryData(meQueryKey, fresh);
      return fresh.has_phone;
    } catch {
      return false; // сетевой сбой одной попытки — пробуем следующую
    }
  });

  // PHONE_REQUIRED, а /me после отправки вступления уже с телефоном — повторяем вступление.
  // Сравнение времени не даёт зациклиться, если кэш и бэкенд расходятся.
  const hasPhoneNow = me?.has_phone === true;
  useEffect(() => {
    if (phoneRequired && hasPhoneNow && meUpdatedAt > submittedAt) onJoin();
  }, [phoneRequired, hasPhoneNow, meUpdatedAt, submittedAt, onJoin]);

  // Уже в группе — сразу в неё, без промежуточного экрана; «Назад» из группы — на «Мои группы».
  useEffect(() => {
    if (alreadyMember && preview) replace(`/events/${preview.event_id}`, { asFirst: true });
  }, [alreadyMember, preview, replace]);

  /** С ввода кода — вернуться на него, по deep link — открыть ввод кода вместо превью. */
  function onOtherCode() {
    if (first) replace('/join');
    else void navigate(-1);
  }

  // Успешное вступление и переход в группу проверяем раньше ошибок — до перехода держим загрузку.
  if ((validCode && isPending) || join.isSuccess || alreadyMember) {
    return (
      <Screen>
        <div aria-busy="true" className={styles.loading}>
          <Skeleton height={80} />
          <Skeleton height={88} />
        </div>
        <div className={styles.footer}>
          <Button disabled arrow="→">
            Вступить
          </Button>
        </div>
      </Screen>
    );
  }

  const fatal = error ?? join.error;
  if (fatal && isUnauthorized(fatal)) return <OpenInTelegramScreen />;
  if (!validCode || (fatal && isNotFound(fatal))) {
    return (
      <Screen center>
        <EmptyState title="Мы не нашли группу" text="Проверьте код приглашения" />
        <div className={styles.footer}>
          <Button arrow="→" onClick={onOtherCode}>
            Ввести другой код
          </Button>
        </div>
      </Screen>
    );
  }
  if (error) return <ErrorScreen text={error.message} onRetry={() => void refetch()} />;
  if (!preview) return null; // недостижимо: без ошибки и загрузки данные есть

  const header = (
    <ScreenHeader
      eyebrow={EYEBROW}
      title={preview.title}
      subtitle={formatDateRange(preview.starts_on, preview.ends_on)}
    />
  );
  if (!preview.join_open || isApiErrorCode(join.error, 'JOIN_CLOSED')) {
    return (
      <Screen>
        {header}
        <div className={styles.content}>
          <EmptyState
            title="Вступление закрыто"
            text="Организатор закрыл вступление в эту группу"
          />
        </div>
        <div className={styles.footer}>
          <Button variant="secondary" onClick={() => void navigate('/')}>
            Мои группы
          </Button>
        </div>
      </Screen>
    );
  }

  return (
    <Screen>
      {header}
      <div className={styles.content}>
        <p className={styles.card}>{HINT}</p>
      </div>
      <div className={styles.footer}>
        <Button
          arrow="→"
          disabled={join.isPending}
          aria-busy={join.isPending || undefined}
          onClick={onJoin}
        >
          Вступить
        </Button>
        {phoneRequired ? (
          <>
            <p className={styles.error} role="alert">
              {PHONE_REQUIRED_TEXT}
            </p>
            <Button
              variant="secondary"
              disabled={sharing}
              aria-busy={sharing || undefined}
              onClick={() => void share()}
            >
              Поделиться номером
            </Button>
          </>
        ) : (
          join.error && (
            <p className={styles.error} role="alert">
              {join.error.message}
            </p>
          )
        )}
      </div>
    </Screen>
  );
}
