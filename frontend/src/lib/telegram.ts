import {
  init,
  miniApp,
  openTelegramLink,
  retrieveLaunchParams,
  retrieveRawInitData,
  viewport,
} from '@telegram-apps/sdk-react';

const PAGE_COLOR = '#eef2f9';

export const BOT_USERNAME: string = import.meta.env.VITE_BOT_USERNAME ?? '';

/** Инициализация SDK и оформление окна Telegram. Вне Telegram ничего не делает и не падает. */
export function initTelegram(): void {
  try {
    init();
  } catch (e) {
    console.warn('Telegram SDK не инициализирован', e);
    return;
  }
  void setupWindow();
}

async function setupWindow(): Promise<void> {
  try {
    if (miniApp.mount.isAvailable()) await miniApp.mount();
    if (miniApp.setHeaderColor.isAvailable()) miniApp.setHeaderColor(PAGE_COLOR);
    if (miniApp.setBackgroundColor.isAvailable()) miniApp.setBackgroundColor(PAGE_COLOR);
    if (miniApp.ready.isAvailable()) miniApp.ready();
  } catch (e) {
    console.warn('Не удалось настроить miniApp', e);
  }
  try {
    if (viewport.mount.isAvailable()) await viewport.mount();
    if (viewport.expand.isAvailable()) viewport.expand();
  } catch (e) {
    console.warn('Не удалось настроить viewport', e);
  }
}

/** Сырая строка `Telegram.WebApp.initData`; `undefined` вне Telegram. */
export function getInitDataRaw(): string | undefined {
  try {
    return retrieveRawInitData() || undefined;
  } catch {
    return undefined;
  }
}

/**
 * `start_param` мини-приложения. Telegram кладёт его в GET-параметр `tgWebAppStartParam`,
 * но не во всех клиентах (и не при любом способе открытия) — запасной источник
 * `initData.start_param`.
 */
export function getStartParamRaw(): string | undefined {
  try {
    const lp = retrieveLaunchParams();
    return lp.tgWebAppStartParam || lp.tgWebAppData?.start_param || undefined;
  } catch {
    return undefined;
  }
}

/** `photo_url` пользователя из initData (запасной вариант аватара). */
export function getInitDataPhotoUrl(): string | undefined {
  try {
    return retrieveLaunchParams().tgWebAppData?.user?.photo_url || undefined;
  } catch {
    return undefined;
  }
}

/** Допустимый payload `?start=` для Telegram: 1–64 символа `A-Za-z0-9_-`. */
const BOT_START_RE = /^[A-Za-z0-9_-]{1,64}$/;

/** Ссылка на бота; корректный `startParam` передаётся как `?start=` (бот узнаёт, например, код группы). */
function botUrl(startParam?: string): string {
  const base = `https://t.me/${BOT_USERNAME}`;
  return startParam && BOT_START_RE.test(startParam) ? `${base}?start=${startParam}` : base;
}

/** Открывает чат с ботом; некорректный `startParam` отбрасывается. */
export function openBot(startParam?: string): void {
  const url = botUrl(startParam);
  try {
    if (openTelegramLink.isAvailable()) {
      openTelegramLink(url);
      return;
    }
  } catch {
    // падаем на обычное открытие ссылки
  }
  window.open(url, '_blank', 'noopener');
}
