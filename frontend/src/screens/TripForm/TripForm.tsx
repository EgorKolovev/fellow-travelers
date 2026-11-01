import { ArrowRight, X } from 'lucide-react';
import { useState } from 'react';
import { useParams } from 'react-router';
import { ApiError, isApiErrorCode, isUnauthorized } from '../../api/errors';
import { useCreateTrip, useDeleteTrip, useEvent, useTrips, useUpdateTrip } from '../../api/hooks';
import type { Trip } from '../../api/types';
import { formatFlightNumber } from '../../lib/format';
import { useBack } from '../../lib/useBack';
import { Button } from '../../ui/Button/Button';
import { ConfirmScreen } from '../../ui/ConfirmScreen/ConfirmScreen';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { Form, FormFields, FormFooter } from '../../ui/Form/Form';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { TextInput } from '../../ui/TextInput/TextInput';
import { EventErrorScreen, NotAdminScreen } from '../ErrorScreen/EventErrorScreen';
import { OpenInTelegramScreen } from '../ErrorScreen/ErrorScreen';
import { LocationPicker } from './LocationPicker';
import {
  apiFieldsToTripForm,
  EMPTY_TRIP_FORM,
  formToTripInput,
  formToTripUpdate,
  TRIP_NUMBER_MAX,
  tripToForm,
  validateTripForm,
  type TripFormField,
  type TripFormValues,
} from './tripForm';
import styles from './TripForm.module.css';

const TRIP_IN_USE_MESSAGE =
  'На этот рейс уже есть заявки — удалить нельзя. Исправьте данные рейса.';

/** Форма рейса: `/events/:id/admin/trips/new` и `/events/:id/admin/trips/:tripId`. */
export function TripForm() {
  const { id: eventId = '', tripId } = useParams();
  const back = useBack(`/events/${eventId}/admin/trips`);
  const event = useEvent(eventId);
  // Рейс для правки — из списка рейсов группы (отдельного GET нет).
  const trips = useTrips(eventId);

  if (event.isPending || (tripId && trips.isPending)) {
    return (
      <Screen>
        <div aria-busy="true">
          <Skeleton height={80} />
          <Skeleton height={54} count={5} />
        </div>
      </Screen>
    );
  }
  const error = event.error ?? (tripId ? trips.error : null);
  if (error) {
    const retry = () => void (event.error ? event.refetch() : trips.refetch());
    return <EventErrorScreen error={error} eventId={eventId} onRetry={retry} />;
  }
  if (!event.data?.is_admin) return <NotAdminScreen eventId={eventId} />;

  if (!tripId) return <TripEditor eventId={eventId} onDone={back} />;
  const trip = trips.data?.find((t) => t.id === tripId);
  if (!trip) {
    return (
      <Screen center>
        <EmptyState title="Рейс не найден" text="Возможно, его уже удалили" />
        <div className={styles.footer}>
          <Button onClick={back}>К рейсам группы</Button>
        </div>
      </Screen>
    );
  }
  return <TripEditor key={trip.id} eventId={eventId} trip={trip} onDone={back} />;
}

interface EditorProps {
  eventId: string;
  /** Нет — создание. */
  trip?: Trip;
  onDone: () => void;
}

function TripEditor({ eventId, trip, onDone }: EditorProps) {
  const [values, setValues] = useState<TripFormValues>(() =>
    trip ? tripToForm(trip) : EMPTY_TRIP_FORM,
  );
  const [submitted, setSubmitted] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const create = useCreateTrip(eventId);
  const update = useUpdateTrip(eventId);
  const remove = useDeleteTrip(eventId);
  const save = trip ? update : create;
  // Серверные ошибки полей гасим, как только поле поменяли.
  const [edited, setEdited] = useState<Set<TripFormField>>(new Set());

  if ([save.error, remove.error].some((e) => e && isUnauthorized(e))) {
    return <OpenInTelegramScreen />;
  }

  const inUse = isApiErrorCode(remove.error, 'TRIP_IN_USE');
  if (confirmDelete && trip && !inUse) {
    return (
      <ConfirmScreen
        title="Удалить рейс?"
        text="Участники больше не смогут выбрать этот рейс"
        confirmLabel="Да, удалить"
        pending={remove.isPending}
        error={remove.error?.message}
        onCancel={() => {
          remove.reset();
          setConfirmDelete(false);
        }}
        onConfirm={() => remove.mutate(trip.id, { onSuccess: onDone })}
      />
    );
  }

  const clientErrors = submitted ? validateTripForm(values) : {};
  const serverErrors = save.error instanceof ApiError ? apiFieldsToTripForm(save.error.fields) : {};
  const errorOf = (field: TripFormField) =>
    clientErrors[field] ?? (edited.has(field) ? undefined : serverErrors[field]);

  function set<K extends TripFormField>(key: K, value: TripFormValues[K]) {
    setValues((v) => ({ ...v, [key]: value }));
    setEdited((s) => new Set(s).add(key));
  }

  function submit() {
    setSubmitted(true);
    if (Object.keys(validateTripForm(values)).length > 0) return;
    setEdited(new Set());
    if (!trip) {
      create.mutate(formToTripInput(values), { onSuccess: onDone });
      return;
    }
    const body = formToTripUpdate(values, trip);
    if (Object.keys(body).length === 0) onDone();
    else update.mutate({ tripId: trip.id, body }, { onSuccess: onDone });
  }

  return (
    <Screen>
      <ScreenHeader
        eyebrow="Администратор"
        title={trip ? `Рейс ${formatFlightNumber(trip.number)}` : 'Новый рейс'}
        subtitle="Время — местное, по часовому поясу аэропорта"
      />
      <Form onSubmit={submit} aria-busy={save.isPending}>
        <FormFields>
          <TextInput
            label="Номер рейса"
            value={values.number}
            onChange={(v) => set('number', v.toUpperCase())}
            placeholder="SU 1148"
            hint="Например: SU 1148"
            maxLength={TRIP_NUMBER_MAX}
            autoCapitalize="characters"
            autoComplete="off"
            error={errorOf('number')}
            required
          />
          <LocationPicker
            label="Аэропорт вылета"
            value={values.departure}
            onChange={(v) => set('departure', v)}
            error={errorOf('departure')}
          />
          <LocationPicker
            label="Аэропорт прилёта"
            value={values.arrival}
            onChange={(v) => set('arrival', v)}
            error={errorOf('arrival')}
          />
          <TextInput
            label="Вылет (местное время аэропорта вылета)"
            type="datetime-local"
            value={values.departure_at}
            onChange={(v) => set('departure_at', v)}
            error={errorOf('departure_at')}
            required
          />
          <TextInput
            label="Прилёт (местное время аэропорта прилёта)"
            type="datetime-local"
            value={values.arrival_at}
            onChange={(v) => set('arrival_at', v)}
            error={errorOf('arrival_at')}
            required
          />
          {trip && (
            <TextInput
              label="Расчётное время прилёта (если рейс задерживается)"
              type="datetime-local"
              value={values.estimated_at}
              onChange={(v) => set('estimated_at', v)}
              hint="Необязательно. Очистите поле, если рейс прилетает по расписанию"
              error={errorOf('estimated_at')}
            />
          )}
        </FormFields>
        <FormFooter>
          <Button type="submit" arrow={<Icon icon={ArrowRight} />} disabled={save.isPending}>
            {trip ? 'Сохранить' : 'Добавить рейс'}
          </Button>
          {save.error && (
            <p className={styles.error} role="alert">
              {save.error.message}
            </p>
          )}
        </FormFooter>
      </Form>
      {trip && (
        <div className={styles.footer}>
          <Button
            variant="danger"
            icon={<Icon icon={X} />}
            onClick={() => {
              remove.reset();
              setConfirmDelete(true);
            }}
          >
            Удалить рейс
          </Button>
          {inUse && (
            <p className={styles.error} role="alert">
              {TRIP_IN_USE_MESSAGE}
            </p>
          )}
        </div>
      )}
    </Screen>
  );
}
