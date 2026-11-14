import { act, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { InitialEntry } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/errors';
import type { Me } from '../../api/me';
import type { Application, Trip } from '../../api/types';
import { EVENT_ID } from '../../test/events';
import { GDZ, SVO, makeTrip } from '../../test/fixtures';
import { renderRoute } from '../../test/render';
import { ApplicationWizard } from './ApplicationWizard';

const api = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  fetchEventTrips: vi.fn(),
  fetchMyApplication: vi.fn(),
  upsertMyApplication: vi.fn(),
}));
/** Обработчик BackButton Telegram, который зарегистрировал экран. */
const backButton = vi.hoisted(() => ({ handler: null as null | (() => void) }));

vi.mock('../../api/me', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/me')>()),
  fetchMe: api.fetchMe,
}));
vi.mock('../../api/trips', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/trips')>()),
  fetchEventTrips: api.fetchEventTrips,
}));
vi.mock('../../api/applications', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/applications')>()),
  fetchMyApplication: api.fetchMyApplication,
  upsertMyApplication: api.upsertMyApplication,
}));
vi.mock('../../lib/useBackButton', () => ({
  useBackButton: (handler: () => void) => {
    backButton.handler = handler;
  },
}));

const GROUP = `/events/${EVENT_ID}`;
const APPLY = `${GROUP}/apply`;

const me: Me = {
  user_id: '7b0e1c0e-6f0e-4a3e-9d5e-1f6f0b8f2a11',
  first_name: 'Егор',
  last_name: 'Макаров',
  username: 'tramakarov',
  phone: '+79122042912',
  photo_url: null,
  telegram_id: 1,
  has_phone: true,
};

const SVX = {
  ...SVO,
  id: '66666666-6666-4666-8666-666666666666',
  code: 'SVX',
  name: 'Кольцово',
  city: 'Екатеринбург',
  timezone: 'Asia/Yekaterinburg',
};

const su1148 = makeTrip();
const su1150 = makeTrip({
  id: '77777777-7777-4777-8777-777777777777',
  number: 'SU1150',
  scheduled_departure: '2026-08-22T06:00:00+03:00',
  scheduled_arrival: '2026-08-22T09:00:00+03:00',
  effective_arrival: '2026-08-22T09:00:00+03:00',
});
// 00:30 по Екатеринбургу 22 августа — по UTC это ещё 21 августа.
const u6night = makeTrip({
  id: '88888888-8888-4888-8888-888888888888',
  number: 'U6115',
  departure_location: SVX,
  arrival_location: GDZ,
  scheduled_departure: '2026-08-21T19:30:00Z',
  scheduled_arrival: '2026-08-22T03:00:00+03:00',
  effective_arrival: '2026-08-22T03:00:00+03:00',
});

function makeApplication(overrides: Partial<Application> = {}): Application {
  return {
    id: '99999999-9999-4999-8999-999999999999',
    event_id: EVENT_ID,
    trip: su1148,
    with_companion: false,
    passengers: 1,
    baggage_count: 2,
    max_wait_minutes: 30,
    status: 'searching',
    at_meeting_point: false,
    departed_at: null,
    transfer_id: null,
    created_at: '2026-08-01T10:00:00+00:00',
    updated_at: '2026-08-01T10:00:00+00:00',
    ...overrides,
  };
}

/** По умолчанию мастер открыт с экрана группы (есть куда вернуться). */
function renderWizard(path: InitialEntry = APPLY, history: string[] = [GROUP]) {
  return renderRoute('/events/:id/apply', <ApplicationWizard />, path, history);
}

async function click(name: string | RegExp) {
  await userEvent.click(await screen.findByRole('button', { name }));
}

async function heading(name: string) {
  return screen.findByRole('heading', { level: 1, name });
}

/** «Далее» на шаге профиля — после загрузки /me (в скелетоне кнопка заблокирована). */
async function profileNext() {
  await screen.findByDisplayValue('Егор Макаров');
  await click(/Далее/);
}

function pressBack() {
  act(() => backButton.handler?.());
}

/** Шаги 1–2 через город: профиль → Москва → рейс. */
async function pickMoscowFlight(trip: Trip = su1148) {
  await profileNext();
  await click('Москва');
  await heading('Выберите рейс');
  await click(new RegExp(trip.number.slice(2)));
}

describe('ApplicationWizard', () => {
  beforeEach(() => {
    backButton.handler = null;
    api.fetchMe.mockReset().mockResolvedValue(me);
    api.fetchEventTrips.mockReset().mockResolvedValue([su1148, su1150, u6night]);
    api.fetchMyApplication
      .mockReset()
      .mockRejectedValue(new ApiError(404, 'NOT_FOUND', 'Заявка не найдена'));
    api.upsertMyApplication
      .mockReset()
      .mockImplementation(async () => makeApplication({ with_companion: false }));
  });

  it('полный проход: город → рейс → один → ручная кладь → 15 мин → PUT → назад к группе', async () => {
    renderWizard();
    expect(await heading('Ваши данные')).toBeInTheDocument();
    expect(screen.getByText('Шаг 1 из 5')).toBeInTheDocument();

    await profileNext();
    expect(await heading('Откуда прилетаете?')).toBeInTheDocument();
    expect(
      screen.getByText('Выберите город, из которого летите рейсом в Геленджик'),
    ).toBeInTheDocument();
    const options = screen.getAllByRole('button').map((b) => b.textContent);
    expect(options).toEqual([
      expect.stringContaining('Найти по номеру рейса'),
      expect.stringContaining('Екатеринбург'),
      expect.stringContaining('Москва'),
    ]);

    await click('Москва');
    expect(await heading('Выберите рейс')).toBeInTheDocument();
    expect(screen.getByText('Москва → Геленджик')).toBeInTheDocument();
    const flights = screen.getAllByRole('button', { name: /SU 11/ }).map((b) => b.textContent);
    expect(flights).toEqual([
      expect.stringContaining('SU 1150 · 22 августа'),
      expect.stringContaining('SU 1148 · 22 августа'),
    ]);

    await click(/SU 1148/);
    expect(await heading('Летите с кем-то?')).toBeInTheDocument();
    expect(screen.getByText('Шаг 3 из 5')).toBeInTheDocument();
    await click(/Нет, лечу один/);
    expect(await heading('Багаж')).toBeInTheDocument();
    await click(/Только ручная кладь/);
    expect(await heading('Сколько готовы ждать попутчиков?')).toBeInTheDocument();
    expect(screen.queryByLabelText('Часы')).not.toBeInTheDocument();
    await click('15 минут');

    expect(await heading('Всё верно?')).toBeInTheDocument();
    expect(screen.getByText('Шаг 5 из 5')).toBeInTheDocument();
    expect(await screen.findByText('Егор Макаров')).toBeInTheDocument();
    expect(screen.getByText('@tramakarov · +7 912 204 29 12')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Рейс SU 1148' })).toBeInTheDocument();
    expect(screen.getByText('Лечу один')).toBeInTheDocument();
    expect(screen.getByText('Только ручная кладь')).toBeInTheDocument();
    expect(screen.getByText('Готов ждать 15 минут')).toBeInTheDocument();

    await click(/Да, всё верно/);
    expect(api.upsertMyApplication).toHaveBeenCalledWith(EVENT_ID, {
      trip_id: su1148.id,
      with_companion: false,
      baggage_count: 0,
      max_wait_minutes: 15,
      seek_companions: true,
    });
    expect(await screen.findByTestId('location')).toHaveTextContent(GROUP);
  });

  it('открыт первым: после отправки — на группу с заменой', async () => {
    const { router } = renderWizard(APPLY, []);
    await pickMoscowFlight();
    await click(/Нет, лечу один/);
    await click(/Только ручная кладь/);
    await click('Не хочу ждать');
    await click(/Да, всё верно/);
    expect(await screen.findByTestId('location')).toHaveTextContent(GROUP);
    expect(router.state.historyAction).toBe('REPLACE');
  });

  it('«Не искать попутчиков» → seek_companions: false', async () => {
    renderWizard();
    await pickMoscowFlight();
    await click(/Не искать попутчиков/);
    await click(/Только ручная кладь/);
    await click('30 минут');
    expect(await screen.findByText('Не ищу попутчиков')).toBeInTheDocument();
    await click(/Да, всё верно/);
    expect(api.upsertMyApplication).toHaveBeenCalledWith(EVENT_ID, {
      trip_id: su1148.id,
      with_companion: false,
      baggage_count: 0,
      max_wait_minutes: 30,
      seek_companions: false,
    });
  });

  describe('поиск по номеру', () => {
    async function search(number: string, date: string) {
      await profileNext();
      await click(/Найти по номеру рейса/);
      const next = await screen.findByRole('button', { name: /Далее/ });
      expect(next).toBeDisabled();
      await userEvent.type(screen.getByLabelText('Номер рейса'), number);
      expect(next).toBeDisabled();
      await userEvent.type(screen.getByLabelText('Дата'), date);
      expect(next).toBeEnabled();
      await userEvent.click(next);
    }

    it('одно совпадение по дате в поясе аэропорта вылета → «Это ваш рейс?»', async () => {
      renderWizard();
      await search('u6 115', '2026-08-22');
      expect(await heading('Это ваш рейс?')).toBeInTheDocument();
      expect(screen.getByRole('region', { name: 'Рейс U6 115' })).toBeInTheDocument();

      // «Нет» — назад к вводу, значения сохранены.
      await click('Нет');
      expect(await screen.findByLabelText('Номер рейса')).toHaveValue('u6 115');
      await click(/Далее/);
      await click('Да');
      expect(await heading('Летите с кем-то?')).toBeInTheDocument();
    });

    it('UTC-дата рейса не находит ночной рейс', async () => {
      renderWizard();
      await search('U6115', '2026-08-21');
      expect(await heading('Мы не нашли рейсы')).toBeInTheDocument();
      expect(
        screen.getByText(
          'Проверьте номер рейса и дату. Если рейса нет в списке — напишите организатору',
        ),
      ).toBeInTheDocument();
      await click(/Назад/);
      expect(await screen.findByLabelText('Номер рейса')).toHaveValue('U6115');
    });

    it('несколько совпадений — список', async () => {
      const second = makeTrip({
        id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        scheduled_departure: '2026-08-22T18:00:00+03:00',
      });
      api.fetchEventTrips.mockResolvedValue([second, su1148]);
      renderWizard();
      await search('SU1148', '2026-08-22');
      expect(await heading('Выберите рейс')).toBeInTheDocument();
      expect(screen.getByText('SU 1148 · 22 августа', { selector: 'p' })).toBeInTheDocument();
      expect(screen.getAllByRole('button', { name: /SU 1148/ })).toHaveLength(2);
    });
  });

  it('багаж: счётчик 1..2, подсказка для +1', async () => {
    renderWizard();
    await pickMoscowFlight();
    await click(/Да, со мной \+1/);
    await click(/Лечу с багажом/);
    expect(await screen.findByText('Багаж считается на вас обоих')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('1');
    expect(screen.getByRole('button', { name: 'Меньше' })).toBeDisabled();
    await click('Больше');
    expect(screen.getByRole('status')).toHaveTextContent('2');
    expect(screen.getByRole('button', { name: 'Больше' })).toBeDisabled();

    // «Назад» — на выбор багажа, затем снова в счётчик.
    await click(/Назад/);
    await click(/Лечу с багажом/);
    await click('Больше');
    await click(/Далее/);
    await click('Не хочу ждать');
    expect(await screen.findByText('2 чемодана')).toBeInTheDocument();
    expect(screen.getByText('Лечу с +1')).toBeInTheDocument();
    await click(/Да, всё верно/);
    expect(api.upsertMyApplication).toHaveBeenCalledWith(
      EVENT_ID,
      expect.objectContaining({ with_companion: true, baggage_count: 2, seek_companions: true }),
    );
  });

  it('багаж: «Не искать попутчиков» из подсказки', async () => {
    renderWizard();
    await pickMoscowFlight();
    await click(/Нет, лечу один/);
    await click(/Лечу с багажом/);
    expect(
      screen.getByText(/В одно такси помещается не больше двух чемоданов/),
    ).toBeInTheDocument();
    await click(/Не искать попутчиков/);
    await click('15 минут');
    expect(await screen.findByText('Не ищу попутчиков')).toBeInTheDocument();
    expect(screen.getByText('1 чемодан')).toBeInTheDocument();
  });

  it('существующая заявка + ?step=review: сводка предзаполнена, правка возвращает в сводку', async () => {
    api.fetchMyApplication.mockResolvedValue(makeApplication());
    renderWizard(`${APPLY}?step=review`);

    expect(await heading('Всё верно?')).toBeInTheDocument();
    expect(screen.getByText('Лечу один')).toBeInTheDocument();
    expect(screen.getByText('2 чемодана')).toBeInTheDocument();
    expect(screen.getByText('Готов ждать 30 минут')).toBeInTheDocument();

    await click('Изменить: Лечу один');
    expect(await heading('Летите с кем-то?')).toBeInTheDocument();
    await click(/Да, со мной \+1/);
    expect(await heading('Всё верно?')).toBeInTheDocument();
    expect(screen.getByText('Лечу с +1')).toBeInTheDocument();

    await click('Изменить рейс');
    await click('Москва');
    await click(/SU 1150/);
    expect(await heading('Всё верно?')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Рейс SU 1150' })).toBeInTheDocument();

    // Правка с отменой: «Назад» из шага — в сводку без изменений.
    await click('Изменить: Готов ждать 30 минут');
    await heading('Сколько готовы ждать попутчиков?');
    pressBack();
    expect(await heading('Всё верно?')).toBeInTheDocument();

    await click(/Да, всё верно/);
    expect(api.upsertMyApplication).toHaveBeenCalledWith(EVENT_ID, {
      trip_id: su1150.id,
      with_companion: true,
      baggage_count: 2,
      max_wait_minutes: 30,
      seek_companions: true,
    });
  });

  describe('«Другое время» ожидания', () => {
    /** Шаги 1–4 и раскрытие «Другое время» на шаге 5. */
    async function openCustomWait() {
      renderWizard();
      await pickMoscowFlight();
      await click(/Нет, лечу один/);
      await click(/Только ручная кладь/);
      await heading('Сколько готовы ждать попутчиков?');
      await click(/Другое время/);
    }

    async function enter(hours: string, minutes: string) {
      const h = screen.getByLabelText('Часы');
      const m = screen.getByLabelText('Минуты');
      await userEvent.clear(h);
      if (hours) await userEvent.type(h, hours);
      await userEvent.clear(m);
      if (minutes) await userEvent.type(m, minutes);
      await click(/Далее/);
    }

    it('1 ч 30 мин → сводка и PUT с max_wait_minutes: 90', async () => {
      await openCustomWait();
      await enter('1', '30');
      expect(await heading('Всё верно?')).toBeInTheDocument();
      expect(screen.getByText('Готов ждать 1 ч 30 мин')).toBeInTheDocument();
      await click(/Да, всё верно/);
      expect(api.upsertMyApplication).toHaveBeenCalledWith(
        EVENT_ID,
        expect.objectContaining({ max_wait_minutes: 90 }),
      );
    });

    it('0:00 и 24:01 не проходят, 24:00 → 1440', async () => {
      await openCustomWait();
      await enter('0', '0');
      expect(screen.getByRole('alert')).toHaveTextContent('От 1 минуты до 24 часов');
      expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
        'Сколько готовы ждать попутчиков?',
      );

      await enter('24', '1');
      expect(screen.getByRole('alert')).toHaveTextContent('От 1 минуты до 24 часов');

      await enter('24', '0');
      expect(await heading('Всё верно?')).toBeInTheDocument();
      expect(screen.getByText('Готов ждать 24 часа')).toBeInTheDocument();
      await click(/Да, всё верно/);
      expect(api.upsertMyApplication).toHaveBeenCalledWith(
        EVENT_ID,
        expect.objectContaining({ max_wait_minutes: 1440 }),
      );
    });

    it('правка заявки с 90 минутами: поля предзаполнены 1 и 30', async () => {
      api.fetchMyApplication.mockResolvedValue(makeApplication({ max_wait_minutes: 90 }));
      renderWizard(`${APPLY}?step=review`);
      await click('Изменить: Готов ждать 1 ч 30 мин');
      await heading('Сколько готовы ждать попутчиков?');
      expect(screen.getByLabelText('Часы')).toHaveValue('1');
      expect(screen.getByLabelText('Минуты')).toHaveValue('30');
    });

    it('правка заявки с готовым вариантом: «Другое время» свёрнуто', async () => {
      api.fetchMyApplication.mockResolvedValue(makeApplication({ max_wait_minutes: 15 }));
      renderWizard(`${APPLY}?step=review`);
      await click('Изменить: Готов ждать 15 минут');
      await heading('Сколько готовы ждать попутчиков?');
      expect(screen.queryByLabelText('Часы')).not.toBeInTheDocument();
    });
  });

  describe('seek_companions из статуса заявки', () => {
    /** Сводка показывает строку попутчиков `companion`; отправка — тело PUT. */
    async function submitReview(
      app: Application,
      companion: string,
      state?: { seekCompanions: boolean },
    ) {
      api.fetchMyApplication.mockResolvedValue(app);
      renderWizard({ pathname: APPLY, search: '?step=review', state });
      expect(await screen.findByText(companion)).toBeInTheDocument();
      await click(/Да, всё верно/);
      expect(api.upsertMyApplication).toHaveBeenCalledTimes(1);
      return api.upsertMyApplication.mock.calls[0]?.[1] as { seek_companions?: boolean };
    }

    it('solo → false: правка не возвращает в поиск', async () => {
      const body = await submitReview(makeApplication({ status: 'solo' }), 'Не ищу попутчиков');
      expect(body.seek_companions).toBe(false);
    });

    it('solo + state.seekCompanions → true', async () => {
      const body = await submitReview(makeApplication({ status: 'solo' }), 'Лечу один', {
        seekCompanions: true,
      });
      expect(body.seek_companions).toBe(true);
    });

    it('cancelled → true', async () => {
      const body = await submitReview(makeApplication({ status: 'cancelled' }), 'Лечу один');
      expect(body.seek_companions).toBe(true);
    });
  });

  it('существующая заявка без ?step: мастер с первого шага, «Назад» с него — выход', async () => {
    api.fetchMyApplication.mockResolvedValue(makeApplication());
    renderWizard();
    expect(await heading('Ваши данные')).toBeInTheDocument();
    pressBack();
    expect(await screen.findByTestId('location')).toHaveTextContent(GROUP);
  });

  it('«Назад» по шагам', async () => {
    renderWizard();
    await pickMoscowFlight();
    await heading('Летите с кем-то?');
    pressBack();
    expect(await heading('Выберите рейс')).toBeInTheDocument();
    pressBack();
    expect(await heading('Откуда прилетаете?')).toBeInTheDocument();
    pressBack();
    expect(await heading('Ваши данные')).toBeInTheDocument();
    pressBack();
    expect(await screen.findByTestId('location')).toHaveTextContent(GROUP);
  });

  it('открыт первым: «Назад» с первого шага — на группу', async () => {
    renderWizard(APPLY, []);
    await heading('Ваши данные');
    pressBack();
    expect(await screen.findByTestId('location')).toHaveTextContent(GROUP);
  });

  it('рейсов нет → пустое состояние и «Обновить»', async () => {
    api.fetchEventTrips.mockResolvedValue([]);
    renderWizard();
    await profileNext();
    expect(await screen.findByText('Организатор ещё не добавил рейсы')).toBeInTheDocument();
    expect(
      screen.getByText('Попробуйте позже или напишите организатору группы'),
    ).toBeInTheDocument();
    api.fetchEventTrips.mockResolvedValue([su1148]);
    await click('Обновить');
    expect(await screen.findByRole('button', { name: 'Москва' })).toBeInTheDocument();
  });

  describe('ошибки отправки', () => {
    async function submitWith(error: ApiError) {
      api.fetchMyApplication.mockResolvedValue(makeApplication());
      api.upsertMyApplication.mockRejectedValue(error);
      renderWizard(`${APPLY}?step=review`);
      await click(/Да, всё верно/);
    }

    it('PHONE_REQUIRED → шаг 1 с сообщением, «Далее» — обратно в сводку', async () => {
      await submitWith(new ApiError(403, 'PHONE_REQUIRED', 'Нужен номер телефона'));
      expect(await heading('Ваши данные')).toBeInTheDocument();
      expect(screen.getByRole('alert')).toHaveTextContent(/поделитесь контактом в чате с ботом/);
      await profileNext();
      expect(await heading('Всё верно?')).toBeInTheDocument();
    });

    it('APPLICATION_LOCKED → сообщение и «К поездке»', async () => {
      await submitWith(
        new ApiError(409, 'APPLICATION_LOCKED', 'Заявку нельзя изменить: группа назначена'),
      );
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'Заявку нельзя изменить: группа назначена',
      );
      await click('К поездке');
      expect(await screen.findByTestId('location')).toHaveTextContent(GROUP);
    });

    it('FORBIDDEN → «вступите в группу»', async () => {
      await submitWith(new ApiError(403, 'FORBIDDEN', 'Нет доступа'));
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'Чтобы подать заявку, вступите в группу',
      );
      expect(screen.getByRole('button', { name: 'К группе' })).toBeInTheDocument();
    });

    it('422 trip_id → выбор рейса с сообщением', async () => {
      await submitWith(
        new ApiError(422, 'VALIDATION_ERROR', 'Неверные данные', { trip_id: 'Рейс не найден' }),
      );
      expect(await heading('Откуда прилетаете?')).toBeInTheDocument();
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Этот рейс больше недоступен — выберите другой',
      );
      await click('Москва');
      await click(/SU 1150/);
      expect(await heading('Всё верно?')).toBeInTheDocument();
    });

    it('прочее → message под кнопкой; во время отправки кнопка заблокирована', async () => {
      api.fetchMyApplication.mockResolvedValue(makeApplication());
      let reject: (e: unknown) => void = () => {};
      api.upsertMyApplication.mockImplementation(
        () =>
          new Promise((_, r) => {
            reject = r;
          }),
      );
      renderWizard(`${APPLY}?step=review`);
      await click(/Да, всё верно/);
      expect(screen.getByRole('button', { name: /Да, всё верно/ })).toBeDisabled();
      act(() => reject(new ApiError(500, 'INTERNAL_ERROR', 'Сервер недоступен')));
      expect(await screen.findByRole('alert')).toHaveTextContent('Сервер недоступен');
      expect(screen.getByRole('button', { name: /Да, всё верно/ })).toBeEnabled();
    });
  });
});
