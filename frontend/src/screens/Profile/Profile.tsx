import { ArrowRight } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { isUnauthorized } from '../../api/errors';
import { fetchMe, meQueryKey } from '../../api/me';
import { GO_TO_BOT_LABEL, SHARE_CONTACT_IN_BOT_TEXT } from '../../lib/botText';
import { formatName, formatPhone } from '../../lib/format';
import { getInitDataPhotoUrl, getStartParamRaw, openBot } from '../../lib/telegram';
import { useBack } from '../../lib/useBack';
import { Avatar } from '../../ui/Avatar/Avatar';
import { Button } from '../../ui/Button/Button';
import { Field, FieldSkeleton } from '../../ui/Field/Field';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { ErrorScreen, OpenInTelegramScreen } from '../ErrorScreen/ErrorScreen';
import styles from './Profile.module.css';

const HEADER = {
  title: 'Ваши данные',
  subtitle: 'Что увидят ваши попутчики',
  step: { current: 1, total: 5 },
};

/** Экран `/profile`: шаг «Ваши данные» сам по себе, «Далее» — на «Мои группы». */
export function Profile() {
  const navigate = useNavigate();
  useBack('/');
  return <ProfileForm onNext={() => void navigate('/')} />;
}

interface Props {
  onNext: () => void;
  /** Сообщение над кнопкой (например, бэкенд ответил PHONE_REQUIRED). */
  notice?: string | null;
}

/** Шаг 1 «Ваши данные»: профиль из /me; без телефона — отправка в бота; «Далее» — только с телефоном. */
export function ProfileForm({ onNext, notice }: Props) {
  const {
    data: me,
    error,
    isPending,
    refetch,
  } = useQuery({
    queryKey: meQueryKey,
    queryFn: fetchMe,
    retry: false,
  });

  if (isPending) {
    return (
      <Screen>
        <ScreenHeader {...HEADER} />
        <div className={styles.form} aria-busy="true">
          <Avatar loading />
          <FieldSkeleton label="Имя" />
          <FieldSkeleton label="Телеграм" />
          <FieldSkeleton label="Номер телефона" />
        </div>
        <div className={styles.footer}>
          <Button disabled arrow={<Icon icon={ArrowRight} />}>
            Далее
          </Button>
        </div>
      </Screen>
    );
  }

  if (error) {
    if (isUnauthorized(error)) return <OpenInTelegramScreen />;
    return <ErrorScreen text={error.message} onRetry={() => void refetch()} />;
  }

  return (
    <Screen>
      <ScreenHeader {...HEADER} />
      <div className={styles.form}>
        <Avatar src={me.photo_url ?? getInitDataPhotoUrl()} />
        <Field label="Имя" value={formatName(me.first_name, me.last_name)} />
        <Field
          label="Телеграм"
          value={me.username ? `@${me.username}` : 'нет'}
          muted={!me.username}
        />
        <Field
          label="Номер телефона"
          value={me.phone ? formatPhone(me.phone) : ''}
          hint={me.has_phone ? undefined : SHARE_CONTACT_IN_BOT_TEXT}
        />
        {!me.has_phone && (
          <Button variant="secondary" onClick={() => openBot(getStartParamRaw())}>
            {GO_TO_BOT_LABEL}
          </Button>
        )}
      </div>
      {notice && (
        <p className={styles.notice} role="alert">
          {notice}
        </p>
      )}
      <div className={styles.footer}>
        <Button disabled={!me.has_phone} arrow={<Icon icon={ArrowRight} />} onClick={onNext}>
          Далее
        </Button>
      </div>
    </Screen>
  );
}
