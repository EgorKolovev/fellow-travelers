import type { Event, EventCreate, EventUpdate, Point } from '../../api/types';
import { isTelegramChatUrl } from '../../lib/links';

/** Значения формы группы — строки как в полях ввода. */
export interface EventFormValues {
  title: string;
  starts_on: string;
  ends_on: string;
  meeting_address: string;
  /** Посадка в такси там же, где сбор (`pickup_point: null`). */
  pickup_same: boolean;
  pickup_address: string;
  destination_address: string;
  meeting_instruction: string;
  chat_url: string;
}

export type EventFormField = Exclude<keyof EventFormValues, 'pickup_same'>;
export type EventFormErrors = Partial<Record<EventFormField, string>>;

export const TITLE_MAX = 200;
export const ADDRESS_MAX = 500;
export const INSTRUCTION_MAX = 2000;

export const CHAT_URL_ERROR =
  'Укажите ссылку на чат в Telegram: https://t.me/имя_чата, https://t.me/+… или https://t.me/joinchat/…';

export const EMPTY_EVENT_FORM: EventFormValues = {
  title: '',
  starts_on: '',
  ends_on: '',
  meeting_address: '',
  pickup_same: true,
  pickup_address: '',
  destination_address: '',
  meeting_instruction: '',
  chat_url: '',
};

/**
 * Группа → значения формы. В ответе `pickup_point` уже подставлен (равен месту сбора, если не задан),
 * поэтому «посадка там же» — когда адреса совпадают.
 */
export function eventToForm(event: Event): EventFormValues {
  const same = event.pickup_point.address === event.meeting_point.address;
  return {
    title: event.title,
    starts_on: event.starts_on,
    ends_on: event.ends_on,
    meeting_address: event.meeting_point.address,
    pickup_same: same,
    pickup_address: same ? '' : event.pickup_point.address,
    destination_address: event.destination.address,
    meeting_instruction: event.meeting_instruction,
    chat_url: event.chat_url ?? '',
  };
}

function required(value: string, message: string, max?: number): string | undefined {
  const v = value.trim();
  if (!v) return message;
  if (max !== undefined && v.length > max) return `Не длиннее ${max} символов`;
  return undefined;
}

export const DATES_ORDER_ERROR = 'Дата окончания раньше даты начала';

/** Окончание раньше начала (обе даты заполнены). Даты `YYYY-MM-DD` сравниваются как строки. */
export function datesOrderError(
  v: Pick<EventFormValues, 'starts_on' | 'ends_on'>,
): string | undefined {
  const starts = v.starts_on.trim();
  const ends = v.ends_on.trim();
  return starts && ends && ends < starts ? DATES_ORDER_ERROR : undefined;
}

/** Проверка на клиенте; пустой объект — можно отправлять. */
export function validateEventForm(v: EventFormValues): EventFormErrors {
  const errors: EventFormErrors = {
    title: required(v.title, 'Укажите название', TITLE_MAX),
    starts_on: required(v.starts_on, 'Укажите дату начала'),
    ends_on: required(v.ends_on, 'Укажите дату окончания'),
    meeting_address: required(v.meeting_address, 'Укажите место сбора', ADDRESS_MAX),
    pickup_address: v.pickup_same
      ? undefined
      : required(v.pickup_address, 'Укажите точку посадки', ADDRESS_MAX),
    destination_address: required(v.destination_address, 'Укажите адрес назначения', ADDRESS_MAX),
    meeting_instruction: required(
      v.meeting_instruction,
      'Напишите инструкцию для участников',
      INSTRUCTION_MAX,
    ),
  };
  if (!errors.starts_on && !errors.ends_on) errors.ends_on = datesOrderError(v);
  const chat = v.chat_url.trim();
  if (chat && !isTelegramChatUrl(chat)) errors.chat_url = CHAT_URL_ERROR;
  return Object.fromEntries(
    Object.entries(errors).filter(([, message]) => message !== undefined),
  ) as EventFormErrors;
}

/** Точка по адресу. Координаты старого адреса при смене адреса неверны, поэтому не переносим. */
function point(address: string): Point {
  return { address: address.trim() };
}

export function formToCreate(v: EventFormValues): EventCreate {
  const chat = v.chat_url.trim();
  return {
    title: v.title.trim(),
    starts_on: v.starts_on,
    ends_on: v.ends_on,
    meeting_point: point(v.meeting_address),
    pickup_point: v.pickup_same ? null : point(v.pickup_address),
    destination: point(v.destination_address),
    meeting_instruction: v.meeting_instruction.trim(),
    chat_url: chat || null,
  };
}

/**
 * Только изменённые поля для PATCH.
 * «Посадка там же» → `pickup_point: null`, пустой чат → `chat_url: null`.
 */
export function formToUpdate(v: EventFormValues, event: Event): EventUpdate {
  const initial = eventToForm(event);
  const body: EventUpdate = {};
  const title = v.title.trim();
  if (title !== initial.title) body.title = title;
  if (v.starts_on !== initial.starts_on) body.starts_on = v.starts_on;
  if (v.ends_on !== initial.ends_on) body.ends_on = v.ends_on;

  const meeting = v.meeting_address.trim();
  const meetingChanged = meeting !== initial.meeting_address;
  if (meetingChanged) body.meeting_point = point(meeting);

  if (v.pickup_same) {
    // Явный null: посадка следует за местом сбора, в том числе после его изменения.
    if (!initial.pickup_same || meetingChanged) body.pickup_point = null;
  } else {
    const pickup = v.pickup_address.trim();
    if (initial.pickup_same || pickup !== initial.pickup_address) {
      body.pickup_point = point(pickup);
    }
  }

  const destination = v.destination_address.trim();
  if (destination !== initial.destination_address) {
    body.destination = point(destination);
  }
  const instruction = v.meeting_instruction.trim();
  if (instruction !== initial.meeting_instruction) body.meeting_instruction = instruction;
  const chat = v.chat_url.trim();
  if (chat !== initial.chat_url) body.chat_url = chat || null;
  return body;
}

const API_FIELD_TO_FORM: Record<string, EventFormField> = {
  title: 'title',
  starts_on: 'starts_on',
  ends_on: 'ends_on',
  meeting_point: 'meeting_address',
  pickup_point: 'pickup_address',
  destination: 'destination_address',
  meeting_instruction: 'meeting_instruction',
  chat_url: 'chat_url',
};

/**
 * `details.fields` из 422 → ошибки полей формы. Ключи бэкенда — путь через точку
 * (`meeting_point.address`), поэтому берём первый сегмент.
 */
export function apiFieldsToForm(fields: Record<string, string> | undefined): EventFormErrors {
  const errors: EventFormErrors = {};
  for (const [key, message] of Object.entries(fields ?? {})) {
    const field = API_FIELD_TO_FORM[key.split('.')[0] ?? ''];
    if (field && !errors[field]) errors[field] = message;
  }
  return errors;
}
