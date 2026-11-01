import { ArrowRight } from 'lucide-react';
import { useState } from 'react';
import { ApiError } from '../../api/errors';
import { Button } from '../../ui/Button/Button';
import { Checkbox } from '../../ui/Checkbox/Checkbox';
import { Form, FormFields, FormFooter } from '../../ui/Form/Form';
import { Icon } from '../../ui/Icon/Icon';
import { TextInput } from '../../ui/TextInput/TextInput';
import {
  ADDRESS_MAX,
  apiFieldsToForm,
  datesOrderError,
  INSTRUCTION_MAX,
  TITLE_MAX,
  validateEventForm,
  type EventFormErrors,
  type EventFormField,
  type EventFormValues,
} from './eventForm';
import styles from './EventForm.module.css';

interface Props {
  initial: EventFormValues;
  submitLabel: string;
  /** Вызывается только с прошедшими проверку значениями. */
  onSubmit: (values: EventFormValues) => void;
  pending?: boolean;
  /** Ошибка отправки: `details.fields` — под поля, `message` — под кнопку. */
  error?: unknown;
}

/** Форма группы: создание и настройки. */
export function EventForm({ initial, submitLabel, onSubmit, pending = false, error }: Props) {
  const [values, setValues] = useState(initial);
  // Ошибки клиента показываем после первой попытки отправки, дальше — по мере ввода.
  // Исключение — порядок дат: его видно сразу, и пока он неверен, отправка заблокирована.
  const [submitted, setSubmitted] = useState(false);
  // Ошибки сервера по полю гасим, как только поле поменяли.
  const [touchedAfterError, setTouchedAfterError] = useState<Set<EventFormField>>(new Set());
  const [lastError, setLastError] = useState<unknown>(error);
  if (error !== lastError) {
    setLastError(error);
    setTouchedAfterError(new Set());
  }

  const datesError = datesOrderError(values);
  const clientErrors: EventFormErrors = submitted
    ? validateEventForm(values)
    : datesError
      ? { ends_on: datesError }
      : {};
  const serverErrors = error instanceof ApiError ? apiFieldsToForm(error.fields) : {};
  const errorOf = (field: EventFormField): string | undefined =>
    clientErrors[field] ?? (touchedAfterError.has(field) ? undefined : serverErrors[field]);

  function set<K extends keyof EventFormValues>(key: K, value: EventFormValues[K]) {
    setValues((v) => ({ ...v, [key]: value }));
    if (key !== 'pickup_same') {
      setTouchedAfterError((s) => new Set(s).add(key as EventFormField));
    }
  }

  function submit() {
    if (datesError) return;
    setSubmitted(true);
    const errors: EventFormErrors = validateEventForm(values);
    if (Object.keys(errors).length === 0) onSubmit(values);
  }

  const message = error instanceof Error ? error.message : undefined;

  return (
    <Form onSubmit={submit} aria-busy={pending}>
      <FormFields>
        <TextInput
          label="Название"
          value={values.title}
          onChange={(v) => set('title', v)}
          placeholder="Например: Конференция в Казани"
          maxLength={TITLE_MAX}
          error={errorOf('title')}
          required
        />
        <div className={styles.dates}>
          <TextInput
            label="Начало"
            type="date"
            value={values.starts_on}
            onChange={(v) => set('starts_on', v)}
            error={errorOf('starts_on')}
            required
          />
          <TextInput
            label="Окончание"
            type="date"
            value={values.ends_on}
            onChange={(v) => set('ends_on', v)}
            min={values.starts_on || undefined}
            error={errorOf('ends_on')}
            required
          />
        </div>
        <TextInput
          label="Место сбора"
          multiline
          rows={2}
          value={values.meeting_address}
          onChange={(v) => set('meeting_address', v)}
          placeholder="Аэропорт, терминал, ориентир"
          maxLength={ADDRESS_MAX}
          error={errorOf('meeting_address')}
          required
        />
        <Checkbox
          label="Посадка в такси там же, где сбор"
          checked={values.pickup_same}
          onChange={(v) => set('pickup_same', v)}
        />
        {!values.pickup_same && (
          <TextInput
            label="Точка посадки"
            multiline
            rows={2}
            value={values.pickup_address}
            onChange={(v) => set('pickup_address', v)}
            maxLength={ADDRESS_MAX}
            error={errorOf('pickup_address')}
            required
          />
        )}
        <TextInput
          label="Адрес назначения"
          multiline
          rows={2}
          value={values.destination_address}
          onChange={(v) => set('destination_address', v)}
          maxLength={ADDRESS_MAX}
          error={errorOf('destination_address')}
          required
        />
        <TextInput
          label="Инструкция для участников"
          multiline
          rows={4}
          value={values.meeting_instruction}
          onChange={(v) => set('meeting_instruction', v)}
          placeholder="Например: после прилёта идите к стойке информации у выхода № 3"
          maxLength={INSTRUCTION_MAX}
          error={errorOf('meeting_instruction')}
          required
        />
        <TextInput
          label="Ссылка на общий чат"
          type="url"
          inputMode="url"
          value={values.chat_url}
          onChange={(v) => set('chat_url', v)}
          placeholder="https://t.me/…"
          hint="Необязательно"
          autoComplete="off"
          error={errorOf('chat_url')}
        />
      </FormFields>
      <FormFooter>
        <Button
          type="submit"
          arrow={<Icon icon={ArrowRight} />}
          disabled={pending || datesError !== undefined}
        >
          {submitLabel}
        </Button>
        {message && (
          <p className={styles.error} role="alert">
            {message}
          </p>
        )}
      </FormFooter>
    </Form>
  );
}
