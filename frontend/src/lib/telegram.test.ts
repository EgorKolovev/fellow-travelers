import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const sdk = vi.hoisted(() => {
  const openTelegramLink = Object.assign(vi.fn(), { isAvailable: vi.fn(() => true) });
  return { retrieveLaunchParams: vi.fn(), openTelegramLink };
});

vi.mock('@telegram-apps/sdk-react', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@telegram-apps/sdk-react')>()),
  retrieveLaunchParams: sdk.retrieveLaunchParams,
  openTelegramLink: sdk.openTelegramLink,
}));

/** BOT_USERNAME читается при загрузке модуля — импортируем после stubEnv. */
async function loadTelegram() {
  vi.resetModules();
  return import('./telegram');
}

describe('getStartParamRaw', () => {
  beforeEach(() => {
    sdk.retrieveLaunchParams.mockReset();
  });

  it('берёт GET-параметр tgWebAppStartParam', async () => {
    sdk.retrieveLaunchParams.mockReturnValue({
      tgWebAppStartParam: 'join_ABC123',
      tgWebAppData: { start_param: 'join_OTHER1' },
    });
    const { getStartParamRaw } = await loadTelegram();
    expect(getStartParamRaw()).toBe('join_ABC123');
  });

  it('без GET-параметра — initData.start_param', async () => {
    sdk.retrieveLaunchParams.mockReturnValue({ tgWebAppData: { start_param: 'join_ABC123' } });
    const { getStartParamRaw } = await loadTelegram();
    expect(getStartParamRaw()).toBe('join_ABC123');
  });

  it('нигде нет или SDK бросает — undefined', async () => {
    const { getStartParamRaw } = await loadTelegram();
    sdk.retrieveLaunchParams.mockReturnValue({ tgWebAppStartParam: '', tgWebAppData: {} });
    expect(getStartParamRaw()).toBeUndefined();
    sdk.retrieveLaunchParams.mockImplementation(() => {
      throw new Error('вне Telegram');
    });
    expect(getStartParamRaw()).toBeUndefined();
  });
});

describe('openBot', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_BOT_USERNAME', 'fellow_bot');
    sdk.openTelegramLink.mockReset();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('передаёт start param боту', async () => {
    const { openBot } = await loadTelegram();
    openBot('join_ABC123');
    expect(sdk.openTelegramLink).toHaveBeenCalledWith('https://t.me/fellow_bot?start=join_ABC123');
  });

  it('без параметра — просто чат с ботом', async () => {
    const { openBot } = await loadTelegram();
    openBot();
    expect(sdk.openTelegramLink).toHaveBeenCalledWith('https://t.me/fellow_bot');
  });

  it.each(['join ABC', 'join_ABC&x=1', 'a'.repeat(65), 'код', ''])(
    'мусорный параметр %j отбрасывается',
    async (param) => {
      const { openBot } = await loadTelegram();
      openBot(param);
      expect(sdk.openTelegramLink).toHaveBeenCalledWith('https://t.me/fellow_bot');
    },
  );
});
