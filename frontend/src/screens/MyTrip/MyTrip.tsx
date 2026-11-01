import { ArrowRight, Pencil, Settings, X } from 'lucide-react';
import { useCallback, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { isUnauthorized } from '../../api/errors';
import {
  useCancelApplication,
  useGoSolo,
  useJoinAsParticipant,
  useMyTrip,
  useResumeSearch,
  useSetMarks,
} from '../../api/hooks';
import { formatDateRange } from '../../lib/datetime';
import { useBackNavigation } from '../../lib/useBack';
import { useBackButton } from '../../lib/useBackButton';
import { useNow } from '../../lib/useNow';
import { Button } from '../../ui/Button/Button';
import { ConfirmScreen } from '../../ui/ConfirmScreen/ConfirmScreen';
import { FlightInfo } from '../../ui/FlightInfo/FlightInfo';
import { Icon } from '../../ui/Icon/Icon';
import { Option } from '../../ui/Option/Option';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import type { WizardLocationState } from '../ApplicationWizard/ApplicationWizard';
import { EventErrorScreen } from '../ErrorScreen/EventErrorScreen';
import { OpenInTelegramScreen } from '../ErrorScreen/ErrorScreen';
import styles from './MyTrip.module.css';
import { arrivalActionsAvailable, tripTitle } from './phase';
import { FellowsSection, MarksSection, MeetSection, TaxiSection } from './sections';

const EYEBROW = 'Ваша поездка';

type Confirm = 'edit' | 'goSolo' | 'cancel' | 'departed';

const CONFIRMS: Record<Confirm, { title: string; text: string; confirmLabel: string }> = {
  edit: {
    title: 'Изменить данные?',
    text: 'Вы выйдете из текущей группы попутчиков. После изменения мы поищем заново',
    confirmLabel: 'Да, изменить',
  },
  goSolo: {
    title: 'Отменить поиск попутчиков?',
    text: 'Мы не будем искать вам попутчиков.',
    confirmLabel: 'Да, поеду один',
  },
  cancel: {
    title: 'Отменить участие?',
    text: 'Вы выйдете из трансфера и не будете учитываться среди прибывающих',
    confirmLabel: 'Да, отменить участие',
  },
  departed: {
    title: 'Отметить отъезд?',
    text: 'Отметку нельзя отменить — после неё изменить поездку не получится',
    confirmLabel: 'Да, я уехал',
  },
};

/** «Моя поездка» `/events/:id`: рейс, как найтись, попутчики, отметки, такси и действия. */
export function MyTrip() {
  const eventId = useParams().id ?? '';
  const navigate = useNavigate();
  const now = useNow();
  const { data: myTrip, error, isPending, isFetching, refetch } = useMyTrip(eventId);
  const event = myTrip?.event;
  const join = useJoinAsParticipant(eventId, {
    enabled: event !== undefined && event.is_admin && !event.is_participant,
  });
  const goSolo = useGoSolo(eventId);
  const resume = useResumeSearch(eventId);
  const cancel = useCancelApplication(eventId);
  const marks = useSetMarks(eventId);
  const pending = goSolo.isPending || resume.isPending || cancel.isPending || marks.isPending;

  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const leave = useBackNavigation('/');
  const back = useCallback(() => {
    if (confirm) {
      setConfirm(null);
      setActionError(null);
    } else {
      leave();
    }
  }, [confirm, leave]);
  useBackButton(back);

  const apply = `/events/${eventId}/apply`;

  /** Мутация с общей обработкой ошибки: текст как есть и перезапрос поездки. */
  function run<V>(
    mutation: { mutate: (v: V, o: { onSuccess: () => void; onError: (e: Error) => void }) => void },
    variables: V,
    onSuccess: () => void = () => setConfirm(null),
  ) {
    setActionError(null);
    mutation.mutate(variables, {
      onSuccess,
      onError: (e) => {
        setActionError(e.message);
        void refetch();
      },
    });
  }

  if (isPending) {
    return (
      <Screen>
        <ScreenHeader eyebrow={EYEBROW} title="Загружаем поездку" />
        <div className={styles.content} aria-busy="true">
          <Skeleton height={160} />
          <Skeleton count={2} />
        </div>
      </Screen>
    );
  }
  if (error) {
    return <EventErrorScreen error={error} eventId={eventId} onRetry={() => void refetch()} />;
  }
  if (join.error && isUnauthorized(join.error)) return <OpenInTelegramScreen />;

  const { application: app, transfer } = myTrip;
  const loadedEvent = myTrip.event;

  if (confirm && app) {
    const texts = CONFIRMS[confirm];
    const onConfirm = {
      edit: () =>
        run(goSolo, undefined, () => {
          const state: WizardLocationState = { seekCompanions: true };
          void navigate(`${apply}?step=review`, { state });
        }),
      goSolo: () => run(goSolo, undefined),
      cancel: () => run(cancel, undefined),
      departed: () => run(marks, { departed: true }),
    }[confirm];
    return (
      <ConfirmScreen
        {...texts}
        onConfirm={onConfirm}
        onCancel={back}
        pending={pending}
        error={actionError}
      />
    );
  }

  const adminLink = loadedEvent.is_admin && (
    <Option
      leading={<Icon icon={Settings} size={24} />}
      title="Управление группой"
      onClick={() => void navigate(`/events/${eventId}/admin`)}
    />
  );
  const dates = formatDateRange(loadedEvent.starts_on, loadedEvent.ends_on);
  const errorLine = actionError && (
    <p className={styles.error} role="alert">
      {actionError}
    </p>
  );

  // Админ без участия: предлагаем вступить по своему приглашению.
  if (!loadedEvent.is_participant) {
    return (
      <Screen>
        <ScreenHeader eyebrow={EYEBROW} title={loadedEvent.title} subtitle={dates} />
        <div className={styles.content}>
          {adminLink}
          <section className={styles.card}>
            <p className={styles.text}>Вы управляете группой. Едете сами? Вступите как участник</p>
            <Button
              disabled={!join.canJoin}
              aria-busy={join.isPending || undefined}
              onClick={() => join.join()}
            >
              Вступить как участник
            </Button>
            {join.error && (
              <p className={styles.error} role="alert">
                {join.error.message}
              </p>
            )}
          </section>
        </div>
      </Screen>
    );
  }

  if (!app) {
    return (
      <Screen>
        <ScreenHeader eyebrow={EYEBROW} title={loadedEvent.title} subtitle={dates} />
        <div className={styles.content}>
          {adminLink}
          <section className={styles.card}>
            <p className={styles.text}>Заполните заявку — мы подберём попутчиков</p>
            <Button arrow={<Icon icon={ArrowRight} />} onClick={() => void navigate(apply)}>
              Заполнить заявку
            </Button>
          </section>
          <MeetSection event={loadedEvent} />
        </div>
      </Screen>
    );
  }

  const departed = app.departed_at !== null;
  const arrivalActions = arrivalActionsAvailable(app, now);
  const canGoSolo = app.status === 'searching' || app.status === 'assigned';

  function editData() {
    if (app?.status === 'assigned') setConfirm('edit');
    else void navigate(`${apply}?step=review`);
  }

  return (
    <Screen>
      <ScreenHeader eyebrow={EYEBROW} title={tripTitle(myTrip, now)} />
      <div className={styles.content}>
        {adminLink}
        <FlightInfo trip={app.trip} baggageCount={app.baggage_count} />
        {departed && (
          <p className={`${styles.plate} ${styles.success}`}>Вы отметили отъезд. Хорошей дороги!</p>
        )}
        <MeetSection event={loadedEvent} />
        <FellowsSection
          app={app}
          transfer={transfer}
          onRefresh={() => void refetch()}
          refreshing={isFetching}
          onResume={() => run(resume, undefined, () => undefined)}
          pending={pending}
        />
        {arrivalActions && (
          <>
            <MarksSection
              app={app}
              onMeetingPoint={(value) => run(marks, { at_meeting_point: value }, () => undefined)}
              onDeparted={() => setConfirm('departed')}
              pending={pending}
            />
            <TaxiSection myTrip={myTrip} />
          </>
        )}
      </div>
      {!departed && (
        <div className={styles.footer}>
          {errorLine}
          <Button
            variant="secondary"
            icon={<Icon icon={Pencil} />}
            onClick={editData}
            disabled={pending}
          >
            Изменить данные
          </Button>
          {canGoSolo && (
            <Button
              variant="danger"
              icon={<Icon icon={X} />}
              onClick={() => setConfirm('goSolo')}
              disabled={pending}
            >
              Поеду один
            </Button>
          )}
          {app.status !== 'cancelled' && (
            <Button variant="secondary" onClick={() => setConfirm('cancel')} disabled={pending}>
              Отменить участие
            </Button>
          )}
        </div>
      )}
      {departed && errorLine}
    </Screen>
  );
}
