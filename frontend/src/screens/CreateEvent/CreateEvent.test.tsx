import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/errors';
import { EVENT_ID, makeEvent } from '../../test/events';
import { renderRoute } from '../../test/render';
import { CreateEvent } from './CreateEvent';

const createEvent = vi.hoisted(() => vi.fn());

vi.mock('../../api/events', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/events')>()),
  createEvent,
}));

function renderCreate() {
  return renderRoute('/events/new', <CreateEvent />, '/events/new', ['/']);
}

async function fillRequired() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText('Название'), 'Слёт');
  await user.type(screen.getByLabelText('Начало'), '2026-08-22');
  await user.type(screen.getByLabelText('Окончание'), '2026-08-24');
  await user.type(screen.getByLabelText('Место сбора'), 'Аэропорт, выход 3');
  await user.type(screen.getByLabelText('Адрес назначения'), 'Отель');
  await user.type(screen.getByLabelText('Инструкция для участников'), 'Ждём у стойки');
  return user;
}

const submit = () => screen.getByRole('button', { name: /Создать группу/ });

describe('CreateEvent', () => {
  beforeEach(() => {
    createEvent.mockReset();
  });

  it('шапка и проверка обязательных полей до отправки', async () => {
    renderCreate();
    expect(screen.getByText('Новая группа')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Создать группу' })).toBeInTheDocument();

    await userEvent.click(submit());
    expect(screen.getByLabelText('Название')).toHaveAccessibleDescription('Укажите название');
    expect(screen.getByLabelText('Место сбора')).toHaveAttribute('aria-invalid', 'true');
    expect(createEvent).not.toHaveBeenCalled();
  });

  it('посадка там же → pickup_point: null; успех → управление группой с заменой', async () => {
    createEvent.mockResolvedValue(makeEvent());
    const { router } = renderCreate();
    const user = await fillRequired();

    await user.click(submit());
    expect(createEvent).toHaveBeenCalledWith({
      title: 'Слёт',
      starts_on: '2026-08-22',
      ends_on: '2026-08-24',
      meeting_point: { address: 'Аэропорт, выход 3' },
      pickup_point: null,
      destination: { address: 'Отель' },
      meeting_instruction: 'Ждём у стойки',
      chat_url: null,
    });
    expect(await screen.findByTestId('location')).toHaveTextContent(`/events/${EVENT_ID}/admin`);
    expect(router.state.historyAction).toBe('REPLACE');
  });

  it('своя точка посадки обязательна', async () => {
    renderCreate();
    const user = await fillRequired();
    await user.click(screen.getByLabelText('Посадка в такси там же, где сбор'));
    await user.click(submit());
    expect(screen.getByLabelText('Точка посадки')).toHaveAccessibleDescription(
      'Укажите точку посадки',
    );
    expect(createEvent).not.toHaveBeenCalled();
  });

  it('чат — только https://t.me/…', async () => {
    createEvent.mockResolvedValue(makeEvent());
    renderCreate();
    const user = await fillRequired();
    const chat = screen.getByLabelText('Ссылка на общий чат');

    await user.type(chat, 'https://example.com/chat');
    await user.click(submit());
    expect(chat).toHaveAccessibleDescription(/Укажите ссылку на чат в Telegram/);
    expect(createEvent).not.toHaveBeenCalled();

    await user.clear(chat);
    await user.type(chat, 'https://t.me/+abc');
    await user.click(submit());
    expect(createEvent).toHaveBeenCalledWith(
      expect.objectContaining({ chat_url: 'https://t.me/+abc' }),
    );
  });

  it('ссылка на прокси t.me — ошибка с примерами допустимых ссылок', async () => {
    renderCreate();
    const user = await fillRequired();
    const chat = screen.getByLabelText('Ссылка на общий чат');

    await user.type(chat, 'https://t.me/proxy?server=1.2.3.4&port=443&secret=abc');
    await user.click(submit());
    expect(chat).toHaveAccessibleDescription(
      expect.stringContaining(
        'Укажите ссылку на чат в Telegram: https://t.me/имя_чата, https://t.me/+… или https://t.me/joinchat/…',
      ),
    );
    expect(chat).toHaveAttribute('aria-invalid', 'true');
    expect(createEvent).not.toHaveBeenCalled();
  });

  it('окончание раньше начала — подсказка сразу, кнопка заблокирована', async () => {
    createEvent.mockResolvedValue(makeEvent());
    renderCreate();
    const user = userEvent.setup();
    const starts = screen.getByLabelText('Начало');
    const ends = screen.getByLabelText('Окончание');

    await user.type(starts, '2026-11-14');
    expect(ends).toHaveAttribute('min', '2026-11-14');
    await user.type(ends, '2026-11-12');
    expect(ends).toHaveAccessibleDescription('Дата окончания раньше даты начала');
    expect(ends).toHaveAttribute('aria-invalid', 'true');
    expect(submit()).toBeDisabled();
    // Остальные ошибки — только после отправки.
    expect(screen.getByLabelText('Название')).not.toHaveAttribute('aria-invalid');
    await user.click(submit());
    expect(createEvent).not.toHaveBeenCalled();

    await user.clear(ends);
    await user.type(ends, '2026-11-15');
    expect(ends).not.toHaveAttribute('aria-invalid');
    expect(screen.queryByText('Дата окончания раньше даты начала')).not.toBeInTheDocument();
    expect(submit()).toBeEnabled();
  });

  it('422: details.fields — под поля, message — под кнопку', async () => {
    createEvent.mockRejectedValue(
      new ApiError(422, 'VALIDATION_ERROR', 'Проверьте введённые данные', {
        'meeting_point.address': 'Слишком длинный адрес',
        ends_on: 'Дата окончания раньше даты начала',
      }),
    );
    renderCreate();
    const user = await fillRequired();
    await user.click(submit());

    expect(await screen.findByRole('alert')).toHaveTextContent('Проверьте введённые данные');
    expect(screen.getByLabelText('Место сбора')).toHaveAccessibleDescription(
      'Слишком длинный адрес',
    );
    expect(screen.getByLabelText('Окончание')).toHaveAccessibleDescription(
      'Дата окончания раньше даты начала',
    );

    // Поле поправили — серверная ошибка под ним пропадает.
    await user.type(screen.getByLabelText('Место сбора'), '!');
    expect(screen.getByLabelText('Место сбора')).not.toHaveAttribute('aria-invalid');
  });
});
