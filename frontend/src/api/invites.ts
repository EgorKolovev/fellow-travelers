import { api } from './client';
import { mockJoinWithoutPhone } from './mock';
import { ensureInitData, unwrap } from './request';
import type { Event, Invite, InvitePreview } from './types';

/** Код и ссылка приглашения (админ). */
export async function fetchEventInvite(eventId: string): Promise<Invite> {
  ensureInitData();
  return unwrap(
    await api.GET('/events/{event_id}/invite', { params: { path: { event_id: eventId } } }),
  );
}

export async function fetchInvitePreview(code: string): Promise<InvitePreview> {
  ensureInitData();
  return unwrap(await api.GET('/invites/{code}', { params: { path: { code } } }));
}

/** Вступить в группу; повторное вступление тоже успешно (200). Без телефона — 403 PHONE_REQUIRED. */
export async function joinByInvite(code: string): Promise<Event> {
  if (import.meta.env.VITE_MOCK_API === 'true' && import.meta.env.VITE_MOCK_NO_PHONE === 'true') {
    mockJoinWithoutPhone();
  }
  ensureInitData();
  return unwrap(await api.POST('/invites/{code}/join', { params: { path: { code } } }));
}
