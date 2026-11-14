import { act, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, NoInitDataError } from '../../api/errors';
import type { Event, InvitePreview } from '../../api/types';
import { useBack } from '../../lib/useBack';
import { renderRoute } from '../../test/render';
import { Invite } from './Invite';

const fetchInvitePreview = vi.hoisted(() => vi.fn());
const joinByInvite = vi.hoisted(() => vi.fn());
const fetchMe = vi.hoisted(() => vi.fn());
const openBot = vi.hoisted(() => vi.fn());

vi.mock('../../api/invites', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/invites')>()),
  fetchInvitePreview,
  joinByInvite,
}));
vi.mock('../../api/me', () => ({ fetchMe, meQueryKey: ['me'] }));
vi.mock('../../lib/telegram', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/telegram')>()),
  getStartParamRaw: () => 'join_K7Q2ZP',
  openBot,
}));

const EVENT_ID = '44444444-4444-4444-8444-444444444444';

const preview: InvitePreview = {
  event_id: EVENT_ID,
  title: 'Слёт в Геленджике',
  starts_on: '2026-08-22',
  ends_on: '2026-08-24',
  join_open: true,
  already_member: false,
};

const point = { address: 'Геленджик, ул. Курортная, 1', lat: null, lon: null };

const event: Event = {
  id: EVENT_ID,
  title: preview.title,
  starts_on: preview.starts_on,
  ends_on: preview.ends_on,
  meeting_point: point,
  pickup_point: point,
  destination: point,
  meeting_instruction: '',
  chat_url: null,
  join_open: true,
  is_admin: false,
  is_participant: true,
};

/** Экран группы с кнопкой «Назад» (как у настоящего — через useBack). */
function GroupStub() {
  const back = useBack('/');
  return <button onClick={back}>Назад из группы</button>;
}

/** По умолчанию — как по deep link: превью открыто первым экраном. */
function renderInvite(history: string[] = []) {
  return renderRoute('/invite/:code', <Invite />, '/invite/k7q2zp', history);
}

/** Ждёт загрузки превью (в скелетоне «Вступить» тоже есть, но disabled) и жмёт «Вступить». */
async function clickJoin() {
  await screen.findByRole('heading', { name: preview.title });
  await userEvent.click(screen.getByRole('button', { name: /Вступить/ }));
}

describe('Invite', () => {
  beforeEach(() => {
    fetchInvitePreview.mockReset();
    joinByInvite.mockReset();
    fetchMe.mockReset();
    openBot.mockReset();
  });

  it('обычное: название, даты, пояснение и «Вступить»; код — в верхнем регистре', async () => {
    fetchInvitePreview.mockResolvedValue(preview);
    renderInvite();

    expect(await screen.findByRole('heading', { name: 'Слёт в Геленджике' })).toBeInTheDocument();
    expect(screen.getByText('Приглашение')).toBeInTheDocument();
    expect(screen.getByText('22–24 августа 2026')).toBeInTheDocument();
    expect(screen.getByText(/После вступления заполните заявку/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Вступить/ })).toBeEnabled();
    expect(fetchInvitePreview).toHaveBeenCalledWith('K7Q2ZP');
  });

  it('успешное вступление → группа с заменой записи', async () => {
    fetchInvitePreview.mockResolvedValue(preview);
    let resolveJoin: (e: Event) => void = () => {};
    joinByInvite.mockReturnValue(new Promise<Event>((r) => (resolveJoin = r)));
    const { router } = renderInvite(['/']);

    await clickJoin();
    expect(screen.getByRole('button', { name: /Вступить/ })).toBeDisabled();
    expect(joinByInvite).toHaveBeenCalledWith('K7Q2ZP');

    resolveJoin(event);
    expect(await screen.findByTestId('location')).toHaveTextContent(`/events/${EVENT_ID}`);
    expect(router.state.historyAction).toBe('REPLACE');
    expect(fetchInvitePreview).toHaveBeenCalledTimes(1);
  });

  it('Home → Join → Invite → вступление → группа → Назад → Home', async () => {
    fetchInvitePreview.mockResolvedValue(preview);
    joinByInvite.mockResolvedValue(event);
    renderRoute(
      '/invite/:code',
      <Invite />,
      '/invite/K7Q2ZP',
      ['/', '/join'],
      [{ path: '/events/:id', element: <GroupStub /> }],
    );

    await clickJoin();
    await userEvent.click(await screen.findByRole('button', { name: 'Назад из группы' }));
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/);
  });

  it('already_member → сразу в группу с заменой записи, без промежуточного экрана', async () => {
    fetchInvitePreview.mockResolvedValue({ ...preview, already_member: true });
    const { router } = renderInvite();

    expect(await screen.findByTestId('location')).toHaveTextContent(`/events/${EVENT_ID}`);
    expect(router.state.historyAction).toBe('REPLACE');
    expect(router.state.location.state).toEqual({ firstEntry: true });
    expect(screen.queryByRole('button', { name: /Вступить/ })).not.toBeInTheDocument();
  });

  it('Home → Join → Invite (already_member) → группа → Назад → Home', async () => {
    fetchInvitePreview.mockResolvedValue({ ...preview, already_member: true });
    renderRoute(
      '/invite/:code',
      <Invite />,
      '/invite/K7Q2ZP',
      ['/', '/join'],
      [{ path: '/events/:id', element: <GroupStub /> }],
    );

    await userEvent.click(await screen.findByRole('button', { name: 'Назад из группы' }));
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/);
  });

  it('join_open=false → «Вступление закрыто» и «Мои группы»', async () => {
    fetchInvitePreview.mockResolvedValue({ ...preview, join_open: false });
    renderInvite();

    expect(await screen.findByText('Вступление закрыто')).toBeInTheDocument();
    expect(screen.getByText('Организатор закрыл вступление в эту группу')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Вступить/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Мои группы' }));
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/);
  });

  it('JOIN_CLOSED на вступлении → «Вступление закрыто»', async () => {
    fetchInvitePreview.mockResolvedValue(preview);
    joinByInvite.mockRejectedValue(
      new ApiError(409, 'JOIN_CLOSED', 'Организатор закрыл вступление'),
    );
    renderInvite();

    await clickJoin();
    expect(await screen.findByText('Вступление закрыто')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Вступить/ })).not.toBeInTheDocument();
  });

  it('прочая ошибка вступления → message под кнопкой, можно повторить', async () => {
    fetchInvitePreview.mockResolvedValue(preview);
    joinByInvite.mockRejectedValue(new ApiError(500, 'INTERNAL_ERROR', 'Сервер недоступен'));
    renderInvite();

    await clickJoin();
    expect(await screen.findByRole('alert')).toHaveTextContent('Сервер недоступен');
    expect(screen.getByRole('button', { name: /Вступить/ })).toBeEnabled();
  });

  it('PHONE_REQUIRED → текст про бота и «Перейти в бота» с start param; вступление не повторяется', async () => {
    fetchInvitePreview.mockResolvedValue(preview);
    joinByInvite.mockRejectedValue(new ApiError(403, 'PHONE_REQUIRED', 'Нужен телефон'));
    renderInvite();

    await clickJoin();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Сначала поделитесь контактом в чате с ботом',
    );
    expect(screen.queryByRole('button', { name: 'Поделиться номером' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Перейти в бота' }));
    expect(openBot).toHaveBeenCalledWith('join_K7Q2ZP');
    expect(joinByInvite).toHaveBeenCalledTimes(1);
    expect(fetchMe).not.toHaveBeenCalled();
  });

  it('404 по deep link → «Мы не нашли группу», «Ввести другой код» открывает ввод кода', async () => {
    fetchInvitePreview.mockRejectedValue(
      new ApiError(404, 'INVITE_NOT_FOUND', 'Приглашение не найдено'),
    );
    const { router } = renderInvite();

    expect(await screen.findByText('Мы не нашли группу')).toBeInTheDocument();
    expect(screen.getByText('Проверьте код приглашения')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Ввести другой код/ }));
    expect(screen.getByTestId('location')).toHaveTextContent('/join');
    expect(router.state.historyAction).toBe('REPLACE');
  });

  it('Home → Join → Invite (404) → «Ввести другой код» → Назад → Home', async () => {
    fetchInvitePreview.mockRejectedValue(
      new ApiError(404, 'INVITE_NOT_FOUND', 'Приглашение не найдено'),
    );
    const { router } = renderInvite(['/', '/join']);

    await userEvent.click(await screen.findByRole('button', { name: /Ввести другой код/ }));
    expect(screen.getByTestId('location')).toHaveTextContent('/join');
    expect(router.state.historyAction).toBe('POP');

    // «Назад» с ввода кода (useBack при наличии истории) — navigate(-1).
    await act(() => router.navigate(-1));
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/);
  });

  it('неверный формат кода → «Мы не нашли группу» без запроса', async () => {
    renderRoute('/invite/:code', <Invite />, '/invite/abc');
    expect(await screen.findByText('Мы не нашли группу')).toBeInTheDocument();
    expect(fetchInvitePreview).not.toHaveBeenCalled();
  });

  it('нет initData → «Откройте приложение из Telegram»', async () => {
    fetchInvitePreview.mockRejectedValue(new NoInitDataError());
    renderInvite();
    expect(
      await screen.findByRole('heading', { name: 'Откройте приложение из Telegram' }),
    ).toBeInTheDocument();
  });

  it('прочая ошибка загрузки → ErrorScreen с повтором', async () => {
    fetchInvitePreview.mockRejectedValueOnce(
      new ApiError(500, 'INTERNAL_ERROR', 'Сервер недоступен'),
    );
    renderInvite();

    expect(await screen.findByText('Сервер недоступен')).toBeInTheDocument();
    fetchInvitePreview.mockResolvedValue(preview);
    await userEvent.click(screen.getByRole('button', { name: /Повторить попытку/ }));
    expect(await screen.findByRole('heading', { name: 'Слёт в Геленджике' })).toBeInTheDocument();
  });
});
