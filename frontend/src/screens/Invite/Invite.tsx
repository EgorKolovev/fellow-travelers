import { ArrowRight } from 'lucide-react';
import { useCallback, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router';
import { isApiErrorCode, isUnauthorized } from '../../api/errors';
import { useInvitePreview, useJoin } from '../../api/hooks';
import { GO_TO_BOT_LABEL, SHARE_CONTACT_IN_BOT_TEXT } from '../../lib/botText';
import { formatDateRange } from '../../lib/datetime';
import { isInviteCode } from '../../lib/inviteCode';
import { getStartParamRaw, openBot } from '../../lib/telegram';
import { useBack, useIsFirstEntry, useReplace } from '../../lib/useBack';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { ErrorScreen, OpenInTelegramScreen } from '../ErrorScreen/ErrorScreen';
import styles from './Invite.module.css';

const EYEBROW = 'Приглашение';

const HINT =
  'После вступления заполните заявку: рейс, багаж и сколько готовы ждать. ' +
  'Мы подберём попутчиков до места встречи.';

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
  const { mutate: mutateJoin } = join;
  /** Вступил: группа становится «первым» экраном — её «Назад» ведёт на «Мои группы», а не на ввод кода. */
  const onJoin = useCallback(() => {
    mutateJoin(code, {
      onSuccess: (event) => replace(`/events/${event.id}`, { asFirst: true }),
    });
  }, [mutateJoin, code, replace]);
  const alreadyMember = preview?.already_member === true;
  const phoneRequired = isApiErrorCode(join.error, 'PHONE_REQUIRED');

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
          <Button disabled arrow={<Icon icon={ArrowRight} />}>
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
          <Button arrow={<Icon icon={ArrowRight} />} onClick={onOtherCode}>
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
          arrow={<Icon icon={ArrowRight} />}
          disabled={join.isPending}
          aria-busy={join.isPending || undefined}
          onClick={onJoin}
        >
          Вступить
        </Button>
        {phoneRequired ? (
          <>
            <p className={styles.error} role="alert">
              {SHARE_CONTACT_IN_BOT_TEXT}
            </p>
            {/* Бот получит тот же start param (join_<код>) и после контакта вернёт в группу. */}
            <Button variant="secondary" onClick={() => openBot(getStartParamRaw())}>
              {GO_TO_BOT_LABEL}
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
