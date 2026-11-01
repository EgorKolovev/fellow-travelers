import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, NoInitDataError } from '../../api/errors';
import type { EventSummary } from '../../api/types';
import { renderRoute } from '../../test/render';
import { Home } from './Home';

const fetchMyEvents = vi.hoisted(() => vi.fn());

vi.mock('../../api/events', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/events')>()),
  fetchMyEvents,
}));

const participant: EventSummary = {
  id: '44444444-4444-4444-8444-444444444444',
  title: 'Слёт в Геленджике',
  starts_on: '2026-08-22',
  ends_on: '2026-08-24',
  is_admin: false,
  is_participant: true,
  my_application_status: 'searching',
};

const adminOnly: EventSummary = {
  id: '77777777-7777-4777-8777-777777777777',
  title: 'Конференция в Казани',
  starts_on: '2026-10-30',
  ends_on: '2026-11-02',
  is_admin: true,
  is_participant: false,
  my_application_status: null,
};

function renderHome(path = '/') {
  return renderRoute('/', <Home />, path);
}

describe('Home', () => {
  beforeEach(() => {
    fetchMyEvents.mockReset();
  });

  it('список: заголовок, даты и статус, вкладка «Все» активна', async () => {
    fetchMyEvents.mockResolvedValue([participant, adminOnly]);
    renderHome();

    expect(await screen.findByRole('button', { name: /Слёт в Геленджике/ })).toHaveTextContent(
      '22–24 августа 2026 · Ищем попутчиков',
    );
    expect(screen.getByRole('button', { name: /Конференция в Казани/ })).toHaveTextContent(
      '30 октября – 2 ноября 2026 · Вы организатор',
    );
    expect(screen.getByRole('tab', { name: 'Все' })).toHaveAttribute('aria-selected', 'true');
  });

  it.each([
    ['assigned', 'Попутчики найдены'],
    ['solo', 'Еду самостоятельно'],
    ['cancelled', 'Участие отменено'],
    [null, 'Заявка не заполнена'],
  ] as const)('статус %s → «%s»', async (status, label) => {
    fetchMyEvents.mockResolvedValue([{ ...participant, my_application_status: status }]);
    renderHome();
    expect(await screen.findByRole('button', { name: /Слёт в Геленджике/ })).toHaveTextContent(
      label,
    );
  });

  it('?filter=admin: активна «Управляю», видны только группы, где я админ', async () => {
    fetchMyEvents.mockResolvedValue([participant, adminOnly]);
    renderHome('/?filter=admin');

    expect(await screen.findByRole('tab', { name: 'Управляю' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    const panel = screen.getByRole('tabpanel');
    expect(within(panel).getByRole('button', { name: /Конференция в Казани/ })).toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: /Слёт/ })).not.toBeInTheDocument();
  });

  it('переключение вкладок', async () => {
    fetchMyEvents.mockResolvedValue([participant]);
    renderHome();

    await userEvent.click(await screen.findByRole('tab', { name: 'Управляю' }));
    expect(screen.getByText('Вы пока не управляете группами')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: 'Все' }));
    expect(screen.getByRole('button', { name: /Слёт в Геленджике/ })).toBeInTheDocument();
  });

  it('участник → «Моя поездка»', async () => {
    fetchMyEvents.mockResolvedValue([participant, adminOnly]);
    renderHome();
    await userEvent.click(await screen.findByRole('button', { name: /Слёт в Геленджике/ }));
    expect(screen.getByTestId('location')).toHaveTextContent(`/events/${participant.id}`);
  });

  it('только админ → управление группой', async () => {
    fetchMyEvents.mockResolvedValue([participant, adminOnly]);
    renderHome();
    await userEvent.click(await screen.findByRole('button', { name: /Конференция в Казани/ }));
    expect(screen.getByTestId('location')).toHaveTextContent(`/events/${adminOnly.id}/admin`);
  });

  it('«Управляю»: админ-участник ведёт в управление группой, иконка-гаечный ключ', async () => {
    const both: EventSummary = { ...participant, is_admin: true };
    fetchMyEvents.mockResolvedValue([both]);
    renderHome('/?filter=admin');

    const card = await screen.findByRole('button', { name: /Слёт в Геленджике/ });
    // Иконка декоративная (aria-hidden): проверяем по классу lucide.
    expect(card.querySelector('.lucide-wrench')).not.toBeNull();
    await userEvent.click(card);
    expect(screen.getByTestId('location')).toHaveTextContent(`/events/${both.id}/admin`);
  });

  it('«Все»: админ-участник ведёт в «Мою поездку», иконка-чемодан', async () => {
    const both: EventSummary = { ...participant, is_admin: true };
    fetchMyEvents.mockResolvedValue([both]);
    renderHome();

    const card = await screen.findByRole('button', { name: /Слёт в Геленджике/ });
    expect(card.querySelector('.lucide-luggage')).not.toBeNull();
    await userEvent.click(card);
    expect(screen.getByTestId('location')).toHaveTextContent(new RegExp(`^/events/${both.id}$`));
  });

  it('пустой список и «Ввести код приглашения» → /join', async () => {
    fetchMyEvents.mockResolvedValue([]);
    renderHome();

    expect(await screen.findByText('Вы пока не в группах')).toBeInTheDocument();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Ввести код приглашения/ }));
    expect(screen.getByTestId('location')).toHaveTextContent('/join');
  });

  it('«Создать группу» → /events/new', async () => {
    fetchMyEvents.mockResolvedValue([participant]);
    renderHome();
    await userEvent.click(await screen.findByRole('button', { name: 'Создать группу' }));
    expect(screen.getByTestId('location')).toHaveTextContent('/events/new');
  });

  it('загрузка — aria-busy', () => {
    fetchMyEvents.mockReturnValue(new Promise(() => {}));
    const { container } = renderHome();
    expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
  });

  it('нет initData → «Откройте приложение из Telegram»', async () => {
    fetchMyEvents.mockRejectedValue(new NoInitDataError());
    renderHome();
    expect(
      await screen.findByRole('heading', { name: 'Откройте приложение из Telegram' }),
    ).toBeInTheDocument();
  });

  it('ошибка → сообщение и повтор', async () => {
    fetchMyEvents.mockRejectedValueOnce(new ApiError(500, 'INTERNAL_ERROR', 'Сервер недоступен'));
    renderHome();

    expect(await screen.findByText('Сервер недоступен')).toBeInTheDocument();
    fetchMyEvents.mockResolvedValue([participant]);
    await userEvent.click(screen.getByRole('button', { name: /Повторить попытку/ }));
    expect(await screen.findByRole('button', { name: /Слёт в Геленджике/ })).toBeInTheDocument();
  });
});
