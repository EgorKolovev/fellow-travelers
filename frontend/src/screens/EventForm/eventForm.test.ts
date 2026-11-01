import { describe, expect, it } from 'vitest';
import { makeEvent } from '../../test/events';
import {
  apiFieldsToForm,
  CHAT_URL_ERROR,
  DATES_ORDER_ERROR,
  datesOrderError,
  EMPTY_EVENT_FORM,
  eventToForm,
  formToCreate,
  formToUpdate,
  validateEventForm,
  type EventFormValues,
} from './eventForm';

const filled: EventFormValues = {
  ...EMPTY_EVENT_FORM,
  title: ' Слёт ',
  starts_on: '2026-08-22',
  ends_on: '2026-08-24',
  meeting_address: 'Аэропорт, выход 3',
  destination_address: 'Отель',
  meeting_instruction: 'Ждём у стойки',
};

describe('datesOrderError', () => {
  it('окончание раньше начала — ошибка', () => {
    expect(datesOrderError({ starts_on: '2026-11-14', ends_on: '2026-11-12' })).toBe(
      'Дата окончания раньше даты начала',
    );
  });

  it.each([
    ['2026-11-14', '2026-11-14'],
    ['2026-11-12', '2026-11-14'],
    ['', '2026-11-12'],
    ['2026-11-14', ''],
    ['', ''],
  ])('без ошибки: начало %j, окончание %j', (starts_on, ends_on) => {
    expect(datesOrderError({ starts_on, ends_on })).toBeUndefined();
  });

  it('тот же текст, что в validateEventForm', () => {
    expect(validateEventForm({ ...filled, ends_on: '2026-08-21' }).ends_on).toBe(DATES_ORDER_ERROR);
  });
});

describe('validateEventForm', () => {
  it('пустая форма — обязательные поля', () => {
    expect(Object.keys(validateEventForm(EMPTY_EVENT_FORM)).sort()).toEqual(
      [
        'destination_address',
        'ends_on',
        'meeting_address',
        'meeting_instruction',
        'starts_on',
        'title',
      ].sort(),
    );
  });

  it('заполненная — без ошибок', () => {
    expect(validateEventForm(filled)).toEqual({});
  });

  it('окончание раньше начала', () => {
    expect(validateEventForm({ ...filled, ends_on: '2026-08-21' })).toEqual({
      ends_on: 'Дата окончания раньше даты начала',
    });
  });

  it('точка посадки обязательна, если посадка не там же', () => {
    expect(validateEventForm({ ...filled, pickup_same: false })).toEqual({
      pickup_address: 'Укажите точку посадки',
    });
  });

  it('чат — только https://t.me/…', () => {
    expect(validateEventForm({ ...filled, chat_url: 'https://evil.com/x' }).chat_url).toBe(
      CHAT_URL_ERROR,
    );
    expect(validateEventForm({ ...filled, chat_url: 'https://t.me/+abc' })).toEqual({});
  });

  it.each([
    'https://t.me/proxy?server=1.2.3.4&port=443&secret=abc',
    'https://t.me/share/url?url=https://evil.com',
    'https://t.me/some_bot?start=abc',
    'https://t.me/chat_room#frag',
    'https://t.me/abcd',
    'https://t.me.evil.com/chat_room',
    'http://t.me/chat_room',
  ])('служебная или чужая ссылка не проходит: %s', (chat_url) => {
    expect(validateEventForm({ ...filled, chat_url }).chat_url).toBe(CHAT_URL_ERROR);
  });

  it.each(['https://t.me/joinchat/abc', 'https://t.me/geleng_chat', ' https://t.me/+abc '])(
    'ссылка на чат проходит: %s',
    (chat_url) => {
      expect(validateEventForm({ ...filled, chat_url })).toEqual({});
    },
  );
});

describe('formToCreate', () => {
  it('посадка там же → pickup_point: null, пустой чат → null, пробелы обрезаны', () => {
    expect(formToCreate(filled)).toEqual({
      title: 'Слёт',
      starts_on: '2026-08-22',
      ends_on: '2026-08-24',
      meeting_point: { address: 'Аэропорт, выход 3' },
      pickup_point: null,
      destination: { address: 'Отель' },
      meeting_instruction: 'Ждём у стойки',
      chat_url: null,
    });
  });

  it('своя точка посадки', () => {
    expect(
      formToCreate({ ...filled, pickup_same: false, pickup_address: 'Парковка P1' }).pickup_point,
    ).toEqual({ address: 'Парковка P1' });
  });
});

describe('formToUpdate', () => {
  const event = makeEvent({ chat_url: 'https://t.me/+chat' });

  it('без изменений — пустое тело', () => {
    expect(formToUpdate(eventToForm(event), event)).toEqual({});
  });

  it('только изменённые поля', () => {
    const v = { ...eventToForm(event), title: 'Новое', ends_on: '2026-08-25' };
    expect(formToUpdate(v, event)).toEqual({ title: 'Новое', ends_on: '2026-08-25' });
  });

  it('очистили чат → chat_url: null', () => {
    expect(formToUpdate({ ...eventToForm(event), chat_url: '' }, event)).toEqual({
      chat_url: null,
    });
  });

  it('отдельная посадка → включили «там же» → pickup_point: null', () => {
    const separate = makeEvent({ pickup_point: { address: 'Парковка P1' } });
    const v = eventToForm(separate);
    expect(v.pickup_same).toBe(false);
    expect(v.pickup_address).toBe('Парковка P1');
    expect(formToUpdate({ ...v, pickup_same: true }, separate)).toEqual({ pickup_point: null });
  });

  it('выключили «там же» → pickup_point с адресом', () => {
    const v = { ...eventToForm(event), pickup_same: false, pickup_address: 'Парковка P1' };
    expect(formToUpdate(v, event)).toEqual({ pickup_point: { address: 'Парковка P1' } });
  });

  it('сменили место сбора при «там же» → и pickup_point: null', () => {
    const v = { ...eventToForm(event), meeting_address: 'Выход 5' };
    expect(formToUpdate(v, event)).toEqual({
      meeting_point: { address: 'Выход 5' },
      pickup_point: null,
    });
  });
});

describe('apiFieldsToForm', () => {
  it('вложенные ключи бэкенда → поля формы', () => {
    expect(
      apiFieldsToForm({
        'meeting_point.address': 'Обязательное поле',
        'pickup_point.address': 'Не длиннее 500 символов',
        ends_on: 'Дата окончания раньше даты начала',
        unknown: 'x',
      }),
    ).toEqual({
      meeting_address: 'Обязательное поле',
      pickup_address: 'Не длиннее 500 символов',
      ends_on: 'Дата окончания раньше даты начала',
    });
  });
});
