import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/errors';
import { EVENT_ID, makeEvent } from '../../test/events';
import { renderRoute } from '../../test/render';
import { EventSettings } from './EventSettings';

const api = vi.hoisted(() => ({ fetchEvent: vi.fn(), updateEvent: vi.fn() }));

vi.mock('../../api/events', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/events')>()),
  fetchEvent: api.fetchEvent,
  updateEvent: api.updateEvent,
}));

const HUB = `/events/${EVENT_ID}/admin`;

function renderSettings() {
  return renderRoute('/events/:id/admin/settings', <EventSettings />, `${HUB}/settings`, [HUB]);
}

describe('EventSettings', () => {
  beforeEach(() => {
    api.fetchEvent.mockReset();
    api.updateEvent.mockReset();
  });

  it('предзаполнение; PATCH только изменённых полей; успех → назад в хаб', async () => {
    const event = makeEvent({ chat_url: 'https://t.me/+chat' });
    api.fetchEvent.mockResolvedValue(event);
    api.updateEvent.mockImplementation(async (_id, body) => ({ ...event, ...body }));
    renderSettings();

    const title = await screen.findByLabelText('Название');
    expect(title).toHaveValue('Слёт в Геленджике');
    expect(screen.getByLabelText('Посадка в такси там же, где сбор')).toBeChecked();
    expect(screen.getByLabelText('Ссылка на общий чат')).toHaveValue('https://t.me/+chat');

    await userEvent.clear(title);
    await userEvent.type(title, 'Слёт 2026');
    await userEvent.clear(screen.getByLabelText('Ссылка на общий чат'));
    await userEvent.click(screen.getByRole('button', { name: /Сохранить/ }));

    expect(api.updateEvent).toHaveBeenCalledWith(EVENT_ID, {
      title: 'Слёт 2026',
      chat_url: null,
    });
    expect(await screen.findByTestId('location')).toHaveTextContent(new RegExp(`^${HUB}$`));
  });

  it('включили «посадка там же» → pickup_point: null', async () => {
    const event = makeEvent({ pickup_point: { address: 'Парковка P1' } });
    api.fetchEvent.mockResolvedValue(event);
    api.updateEvent.mockResolvedValue(event);
    renderSettings();

    expect(await screen.findByLabelText('Точка посадки')).toHaveValue('Парковка P1');
    await userEvent.click(screen.getByLabelText('Посадка в такси там же, где сбор'));
    await userEvent.click(screen.getByRole('button', { name: /Сохранить/ }));
    expect(api.updateEvent).toHaveBeenCalledWith(EVENT_ID, { pickup_point: null });
  });

  it('без изменений — без запроса, назад в хаб', async () => {
    api.fetchEvent.mockResolvedValue(makeEvent());
    renderSettings();
    await userEvent.click(await screen.findByRole('button', { name: /Сохранить/ }));
    expect(api.updateEvent).not.toHaveBeenCalled();
    expect(screen.getByTestId('location')).toHaveTextContent(new RegExp(`^${HUB}$`));
  });

  it('«Закрыть вступление» → подтверждение → PATCH {join_open:false}', async () => {
    api.fetchEvent.mockResolvedValue(makeEvent());
    api.updateEvent.mockResolvedValue(makeEvent({ join_open: false }));
    renderSettings();

    await userEvent.click(await screen.findByRole('button', { name: 'Закрыть вступление' }));
    expect(screen.getByRole('heading', { name: 'Закрыть вступление?' })).toBeInTheDocument();
    expect(
      screen.getByText('Новые участники не смогут присоединиться, текущие сохранят доступ'),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Закрыть вступление' }));

    expect(api.updateEvent).toHaveBeenCalledWith(EVENT_ID, { join_open: false });
    expect(await screen.findByRole('button', { name: 'Открыть вступление' })).toBeInTheDocument();
  });

  it('подтверждение можно отменить', async () => {
    api.fetchEvent.mockResolvedValue(makeEvent());
    renderSettings();
    await userEvent.click(await screen.findByRole('button', { name: 'Закрыть вступление' }));
    await userEvent.click(screen.getByRole('button', { name: 'Нет, вернуться обратно' }));
    expect(screen.getByLabelText('Название')).toBeInTheDocument();
    expect(api.updateEvent).not.toHaveBeenCalled();
  });

  it('«Открыть вступление» → PATCH {join_open:true} без подтверждения', async () => {
    api.fetchEvent.mockResolvedValue(makeEvent({ join_open: false }));
    api.updateEvent.mockResolvedValue(makeEvent({ join_open: true }));
    renderSettings();

    await userEvent.click(await screen.findByRole('button', { name: 'Открыть вступление' }));
    expect(api.updateEvent).toHaveBeenCalledWith(EVENT_ID, { join_open: true });
    expect(await screen.findByRole('button', { name: 'Закрыть вступление' })).toBeInTheDocument();
  });

  it('422 при сохранении — ошибки под полями, остаёмся на экране', async () => {
    api.fetchEvent.mockResolvedValue(makeEvent());
    api.updateEvent.mockRejectedValue(
      new ApiError(422, 'VALIDATION_ERROR', 'Проверьте введённые данные', {
        ends_on: 'Дата окончания раньше даты начала',
      }),
    );
    renderSettings();
    const title = await screen.findByLabelText('Название');
    await userEvent.type(title, '!');
    await userEvent.click(screen.getByRole('button', { name: /Сохранить/ }));

    expect(await screen.findByLabelText('Окончание')).toHaveAccessibleDescription(
      'Дата окончания раньше даты начала',
    );
    expect(screen.queryByTestId('location')).not.toBeInTheDocument();
  });

  it('окончание раньше начала — подсказка сразу, кнопка заблокирована, без запроса', async () => {
    api.fetchEvent.mockResolvedValue(makeEvent());
    api.updateEvent.mockResolvedValue(makeEvent());
    renderSettings();
    const user = userEvent.setup();
    const starts = await screen.findByLabelText('Начало');
    const ends = screen.getByLabelText('Окончание');
    const save = () => screen.getByRole('button', { name: /Сохранить/ });

    await user.clear(starts);
    await user.type(starts, '2026-11-14');
    await user.clear(ends);
    await user.type(ends, '2026-11-12');
    expect(ends).toHaveAttribute('min', '2026-11-14');
    expect(ends).toHaveAccessibleDescription('Дата окончания раньше даты начала');
    expect(save()).toBeDisabled();
    await user.click(save());
    expect(api.updateEvent).not.toHaveBeenCalled();

    await user.clear(ends);
    await user.type(ends, '2026-11-16');
    expect(screen.queryByText('Дата окончания раньше даты начала')).not.toBeInTheDocument();
    expect(save()).toBeEnabled();
    await user.click(save());
    expect(api.updateEvent).toHaveBeenCalledWith(EVENT_ID, {
      starts_on: '2026-11-14',
      ends_on: '2026-11-16',
    });
  });

  it('не админ → «только организатор»', async () => {
    api.fetchEvent.mockResolvedValue(makeEvent({ is_admin: false, is_participant: true }));
    renderSettings();
    expect(
      await screen.findByText('Управлять группой может только организатор'),
    ).toBeInTheDocument();
  });
});
