import { ApiError } from './errors';
import type { components } from './schema';

type Me = components['schemas']['Me'];

/** Фиктивный `Me` для разработки без бэкенда (VITE_MOCK_API=true). */
export async function mockFetchMe(noPhone: boolean): Promise<Me> {
  await new Promise((resolve) => setTimeout(resolve, 400));
  return {
    user_id: '7b0e1c0e-6f0e-4a3e-9d5e-1f6f0b8f2a11',
    first_name: 'Иван',
    last_name: 'Петров',
    username: 'ivan_petrov',
    phone: noPhone ? null : '+79122042912',
    photo_url: null,
    telegram_id: 99281932,
    has_phone: !noPhone,
  };
}

/**
 * Вступление без телефона (VITE_MOCK_API=true и VITE_MOCK_NO_PHONE=true) — 403 PHONE_REQUIRED
 * по контракту. С телефоном мока нет: запрос уходит на бэкенд.
 */
export function mockJoinWithoutPhone(): never {
  throw new ApiError(403, 'PHONE_REQUIRED', 'Чтобы вступить в группу, поделитесь номером телефона');
}
