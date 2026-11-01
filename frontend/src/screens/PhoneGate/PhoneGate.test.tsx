import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Me } from '../../api/me';
import type { InvitePreview } from '../../api/types';

const fetchMe = vi.hoisted(() => vi.fn());
const fetchMyEvents = vi.hoisted(() => vi.fn());
const fetchInvitePreview = vi.hoisted(() => vi.fn());
const tg = vi.hoisted(() => ({
  startParam: undefined as string | undefined,
  canRequestContact: vi.fn(() => false),
  openBot: vi.fn(),
}));

vi.mock('../../api/me', () => ({ fetchMe, meQueryKey: ['me'] }));
vi.mock('../../api/events', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/events')>()),
  fetchMyEvents,
}));
vi.mock('../../api/invites', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/invites')>()),
  fetchInvitePreview,
}));
vi.mock('../../lib/telegram', () => ({
  getStartParamRaw: () => tg.startParam,
  getInitDataRaw: () => 'raw-init-data',
  getInitDataPhotoUrl: () => undefined,
  canRequestContact: tg.canRequestContact,
  askForContact: vi.fn(),
  openBot: tg.openBot,
}));

const me: Me = {
  user_id: '7b0e1c0e-6f0e-4a3e-9d5e-1f6f0b8f2a11',
  first_name: 'Иван',
  last_name: 'Петров',
  username: 'ivan_petrov',
  phone: '+79122042912',
  photo_url: null,
  telegram_id: 1,
  has_phone: true,
};
const noPhone: Me = { ...me, phone: null, has_phone: false };

const preview: InvitePreview = {
  event_id: '44444444-4444-4444-8444-444444444444',
  title: 'Слёт в Геленджике',
  starts_on: '2026-08-22',
  ends_on: '2026-08-24',
  join_open: true,
  already_member: false,
};

/** App читает start param и создаёт роутер при загрузке модуля — грузим заново на каждый тест. */
async function renderApp(startParam?: string) {
  tg.startParam = startParam;
  vi.resetModules();
  const { App } = await import('../../App');
  return render(<App />);
}

/**
 * Ошибка API из текущего реестра модулей: после resetModules класс ApiError у App свой,
 * и `instanceof` с классом, импортированным в тест статически, не сработает.
 */
async function apiError(status: number, code: string, message: string) {
  const { ApiError } = await import('../../api/errors');
  return new ApiError(status, code, message);
}

const gateHeading = () => screen.findByRole('heading', { name: 'Нужен номер телефона' });

describe('PhoneGate', () => {
  beforeEach(() => {
    fetchMe.mockReset();
    fetchMyEvents.mockReset().mockResolvedValue([]);
    fetchInvitePreview.mockReset().mockResolvedValue(preview);
    tg.canRequestContact.mockReset().mockReturnValue(false);
    tg.openBot.mockReset();
  });

  it('без телефона «Мои группы» закрыты гейтом', async () => {
    fetchMe.mockResolvedValue(noPhone);
    await renderApp();

    expect(await gateHeading()).toBeInTheDocument();
    expect(screen.getByText(/Без него нельзя вступить в группу/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Поделиться номером/ })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Проверить ещё раз' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Мои группы' })).not.toBeInTheDocument();
    expect(fetchMyEvents).not.toHaveBeenCalled();
  });

  it('приглашение по start param закрыто гейтом, после телефона открывается именно оно', async () => {
    fetchMe.mockResolvedValueOnce(noPhone).mockResolvedValue(me);
    await renderApp('join_ABC123');

    expect(await gateHeading()).toBeInTheDocument();
    expect(fetchInvitePreview).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Проверить ещё раз' }));

    expect(await screen.findByRole('heading', { name: preview.title })).toBeInTheDocument();
    expect(fetchInvitePreview).toHaveBeenCalledWith('ABC123');
    expect(screen.queryByRole('heading', { name: 'Нужен номер телефона' })).not.toBeInTheDocument();
  });

  it('без requestContact «Поделиться номером» открывает бота с тем же start param', async () => {
    fetchMe.mockResolvedValue(noPhone);
    await renderApp('join_ABC123');

    await gateHeading();
    await userEvent.click(screen.getByRole('button', { name: /Поделиться номером/ }));
    expect(tg.openBot).toHaveBeenCalledWith('join_ABC123');
  });

  it('с телефоном — сразу экран из start param', async () => {
    fetchMe.mockResolvedValue(me);
    await renderApp('join_ABC123');

    expect(await screen.findByRole('heading', { name: preview.title })).toBeInTheDocument();
  });

  it('401 → «Откройте приложение из Telegram»', async () => {
    fetchMe.mockImplementation(async () => {
      throw await apiError(401, 'UNAUTHORIZED', 'Нет доступа');
    });
    await renderApp();

    expect(
      await screen.findByRole('heading', { name: 'Откройте приложение из Telegram' }),
    ).toBeInTheDocument();
  });

  it('другая ошибка → ErrorScreen с повтором', async () => {
    fetchMe
      .mockImplementationOnce(async () => {
        throw await apiError(403, 'FORBIDDEN', 'Сервер недоволен');
      })
      .mockResolvedValue(me);
    await renderApp();

    expect(await screen.findByText('Сервер недоволен')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Повторить попытку/ }));
    expect(await screen.findByRole('heading', { name: 'Мои группы' })).toBeInTheDocument();
  });
});
