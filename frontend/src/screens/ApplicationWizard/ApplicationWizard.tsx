import { useCallback, useState } from 'react';
import { useLocation, useParams, useSearchParams } from 'react-router';
import { isApiErrorCode } from '../../api/errors';
import { useMyApplication, useUpsertApplication } from '../../api/hooks';
import type { Application, Trip } from '../../api/types';
import { SHARE_CONTACT_IN_BOT_TEXT } from '../../lib/botText';
import { useBackNavigation } from '../../lib/useBack';
import { useBackButton } from '../../lib/useBackButton';
import { Screen } from '../../ui/Screen/Screen';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { EventErrorScreen } from '../ErrorScreen/EventErrorScreen';
import { ProfileForm } from '../Profile/Profile';
import styles from './ApplicationWizard.module.css';
import { BaggageCountStep, BaggageStep, CompanionStep, WaitStep } from './ChoiceSteps';
import { applyCompanionChoice, draftFromApplication, draftToInput, type Draft } from './draft';
import { FlightStep, type FlightView, type NumberQuery } from './FlightStep';
import { ReviewStep, type ReviewSection, type SubmitError } from './ReviewStep';
import { WizardHeader } from './WizardHeader';

/** Экран мастера. Шаги — внутреннее состояние, а не записи истории роутера. */
type View =
  | { name: 'profile' }
  | FlightView
  | { name: 'companion' }
  | { name: 'baggage' }
  | { name: 'baggageCount' }
  | { name: 'wait' }
  | { name: 'review' };

interface WizardState {
  /** Пройденные экраны; «Назад» снимает верхний, с последнего — выход из мастера. */
  stack: View[];
  draft: Draft;
  query: NumberQuery;
  /** Сообщение на текущем экране (PHONE_REQUIRED, недоступный рейс). */
  notice: string | null;
}

const SECTION_VIEW: Record<ReviewSection, View> = {
  profile: { name: 'profile' },
  flight: { name: 'cities' },
  companion: { name: 'companion' },
  baggage: { name: 'baggage' },
  wait: { name: 'wait' },
};

const TRIP_UNAVAILABLE = 'Этот рейс больше недоступен — выберите другой';
const JOIN_FIRST = 'Чтобы подать заявку, вступите в группу';

/** `location.state` при открытии мастера: `seekCompanions` — предзаполнить «искать попутчиков». */
export interface WizardLocationState {
  seekCompanions?: boolean;
}

function initialState(
  app: Application | undefined,
  openReview: boolean,
  seekCompanions: boolean,
): WizardState {
  const draft = draftFromApplication(app, { seekCompanions });
  const reviewReady = openReview && draftToInput(draft) !== null;
  return {
    stack: [reviewReady ? { name: 'review' } : { name: 'profile' }],
    draft,
    query: { number: '', date: '' },
    notice: null,
  };
}

/**
 * Следующий экран: если мастер открыт правкой из сводки (сводка есть в стеке) —
 * сразу обратно в сводку, иначе — дальше по шагам.
 */
function advance(stack: View[], next: View): View[] {
  const review = stack.findIndex((v) => v.name === 'review');
  return review >= 0 ? stack.slice(0, review + 1) : [...stack, next];
}

/** Мастер заявки `/events/:id/apply` (`?step=review` — сразу сводка для существующей заявки). */
export function ApplicationWizard() {
  const eventId = useParams().id ?? '';
  const [searchParams] = useSearchParams();
  const openReview = searchParams.get('step') === 'review';
  const locationState = useLocation().state as WizardLocationState | null;
  const seekCompanions = locationState?.seekCompanions === true;
  // Выход из мастера: туда, откуда открыли, или на «Мою поездку» при открытии первым.
  const leave = useBackNavigation(`/events/${eventId}`);
  const application = useMyApplication(eventId);
  // 404 — заявки ещё нет: черновик пустой.
  const noApplication = isApiErrorCode(application.error, 'NOT_FOUND');
  const settled = application.isSuccess || noApplication;

  const [state, setState] = useState<WizardState | null>(null);
  // Черновик заполняем один раз, когда стало известно, есть ли заявка.
  if (state === null && settled) {
    setState(initialState(application.data, openReview, seekCompanions));
  }

  const upsert = useUpsertApplication(eventId);
  const [submitError, setSubmitError] = useState<SubmitError | null>(null);

  const back = useCallback(() => {
    if (!state || state.stack.length <= 1) {
      leave();
      return;
    }
    setSubmitError(null);
    setState({ ...state, stack: state.stack.slice(0, -1), notice: null });
  }, [state, leave]);
  useBackButton(back);

  if (!state) {
    if (application.error && !noApplication) {
      return (
        <EventErrorScreen
          error={application.error}
          eventId={eventId}
          onRetry={() => void application.refetch()}
        />
      );
    }
    return (
      <Screen>
        <WizardHeader step={1} title="Ваши данные" />
        <div className={styles.content} aria-busy="true">
          <Skeleton count={3} />
        </div>
      </Screen>
    );
  }

  const { stack, draft, query, notice } = state;
  const view = stack[stack.length - 1] ?? { name: 'profile' };

  function update(patch: Partial<WizardState>) {
    setSubmitError(null);
    setState((prev) => (prev ? { ...prev, notice: null, ...patch } : prev));
  }

  function open(next: View) {
    update({ stack: [...stack, next] });
  }

  function proceed(nextDraft: Draft, next: View) {
    update({ draft: nextDraft, stack: advance(stack, next) });
  }

  function pickTrip(trip: Trip) {
    proceed({ ...draft, trip }, { name: 'companion' });
  }

  function submit() {
    const input = draftToInput(draft);
    if (!input) return;
    setSubmitError(null);
    upsert.mutate(input, {
      onSuccess: leave,
      onError: (error) => {
        if (isApiErrorCode(error, 'PHONE_REQUIRED')) {
          update({ stack: [...stack, { name: 'profile' }], notice: SHARE_CONTACT_IN_BOT_TEXT });
        } else if (isApiErrorCode(error, 'VALIDATION_ERROR') && error.fields?.trip_id) {
          update({ stack: [...stack, { name: 'cities' }], notice: TRIP_UNAVAILABLE });
        } else if (
          isApiErrorCode(error, 'APPLICATION_LOCKED') ||
          isApiErrorCode(error, 'ALREADY_DEPARTED')
        ) {
          setSubmitError({
            message: error.message,
            action: { label: 'К поездке', onClick: leave },
          });
        } else if (isApiErrorCode(error, 'FORBIDDEN')) {
          setSubmitError({ message: JOIN_FIRST, action: { label: 'К группе', onClick: leave } });
        } else {
          setSubmitError({ message: error.message });
        }
      },
    });
  }

  switch (view.name) {
    case 'profile':
      return (
        <ProfileForm
          notice={notice}
          onNext={() => update({ stack: advance(stack, { name: 'cities' }) })}
        />
      );

    case 'cities':
    case 'city':
    case 'number':
    case 'search':
      return (
        <FlightStep
          eventId={eventId}
          view={view}
          query={query}
          onQueryChange={(q) => update({ query: q })}
          onOpen={open}
          onPick={pickTrip}
          onBack={back}
          notice={notice}
        />
      );

    case 'companion':
      return (
        <CompanionStep
          onChoose={(choice) => proceed(applyCompanionChoice(draft, choice), { name: 'baggage' })}
        />
      );

    case 'baggage':
      return (
        <BaggageStep
          onHandLuggage={() => proceed({ ...draft, baggageCount: 0 }, { name: 'wait' })}
          onWithBaggage={() => open({ name: 'baggageCount' })}
        />
      );

    case 'baggageCount':
      return (
        <BaggageCountStep
          initial={draft.baggageCount ?? 1}
          withCompanion={draft.withCompanion === true}
          seeking={draft.seek !== false}
          onNext={(count) => proceed({ ...draft, baggageCount: count }, { name: 'wait' })}
          onNoSeek={(count) =>
            proceed({ ...draft, baggageCount: count, seek: false }, { name: 'wait' })
          }
          onBack={back}
        />
      );

    case 'wait':
      return (
        <WaitStep
          value={draft.maxWait}
          onChoose={(minutes) => proceed({ ...draft, maxWait: minutes }, { name: 'review' })}
        />
      );

    case 'review': {
      const input = draftToInput(draft);
      // Недостижимо: в сводку попадаем только с полным черновиком (после шага 5 или из заявки).
      if (!draft.trip || !input) return null;
      return (
        <ReviewStep
          trip={draft.trip}
          input={input}
          onEdit={(section) => open(SECTION_VIEW[section])}
          onSubmit={submit}
          pending={upsert.isPending}
          error={submitError}
        />
      );
    }
  }
}
