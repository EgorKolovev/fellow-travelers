import type { Application, ApplicationInput, Trip, WaitMinutes } from '../../api/types';

/** Больше двух мест багажа в одно такси не поместится (контракт: вместимость 2 места). */
export const MAX_BAGGAGE = 2;

/** Готовы ждать попутчиков не больше суток (контракт: `WaitMinutes` 0..1440). */
export const MAX_WAIT_MINUTES = 24 * 60;

/** Готовые варианты шага «Сколько готовы ждать»; остальное — «Другое время». */
export const WAIT_PRESETS: readonly WaitMinutes[] = [0, 15, 30];

/** Поля «Часы» / «Минуты» шага «Другое время» (строки как в инпутах). */
export interface WaitParts {
  hours: string;
  minutes: string;
}

/** 90 → `{ hours: '1', minutes: '30' }`. */
export function splitWait(total: number): WaitParts {
  return { hours: String(Math.floor(total / 60)), minutes: String(total % 60) };
}

const DIGITS_RE = /^\d+$/;

/**
 * Минуты из полей «Часы» / «Минуты»; пустое поле — 0. `null`, если значение не подходит:
 * не целое неотрицательное, минут больше 59 или сумма вне 1..1440.
 */
export function joinWait({ hours, minutes }: WaitParts): WaitMinutes | null {
  const h = hours.trim() || '0';
  const m = minutes.trim() || '0';
  if (!DIGITS_RE.test(h) || !DIGITS_RE.test(m)) return null;
  const mm = Number(m);
  if (mm > 59) return null;
  const total = Number(h) * 60 + mm;
  return total >= 1 && total <= MAX_WAIT_MINUTES ? total : null;
}

/** Черновик заявки в мастере: поля заполняются по шагам. */
export interface Draft {
  trip?: Trip;
  withCompanion?: boolean;
  /** Искать попутчиков; `false` — «Не искать попутчиков» (заявка сразу `solo`). */
  seek?: boolean;
  baggageCount?: number;
  maxWait?: WaitMinutes;
}

/** Выбор на шаге «Летите с кем-то?». */
export type CompanionChoice = 'alone' | 'plusOne' | 'noSeek';

export function companionChoice(draft: Draft): CompanionChoice | undefined {
  if (draft.seek === false) return 'noSeek';
  if (draft.withCompanion === undefined) return undefined;
  return draft.withCompanion ? 'plusOne' : 'alone';
}

export function applyCompanionChoice(draft: Draft, choice: CompanionChoice): Draft {
  switch (choice) {
    case 'alone':
      return { ...draft, withCompanion: false, seek: true };
    case 'plusOne':
      return { ...draft, withCompanion: true, seek: true };
    case 'noSeek':
      return { ...draft, withCompanion: false, seek: false };
  }
}

/**
 * Черновик из существующей заявки. `seek` — `false` только для `solo`: правка данных у того,
 * кто едет самостоятельно, не возвращает его в поиск. `seekCompanions` — явный запрос вернуться
 * в поиск (экран поездки: `go-solo` перед правкой назначенной заявки).
 */
export function draftFromApplication(
  app: Application | undefined,
  { seekCompanions = false }: { seekCompanions?: boolean } = {},
): Draft {
  if (!app) return {};
  return {
    trip: app.trip,
    withCompanion: app.with_companion,
    seek: seekCompanions || app.status !== 'solo',
    baggageCount: Math.min(app.baggage_count, MAX_BAGGAGE),
    maxWait: app.max_wait_minutes,
  };
}

/** Тело PUT из полного черновика (`seek_companions` — всегда явно); `null`, если чего-то не хватает. */
export function draftToInput(draft: Draft): ApplicationInput | null {
  const { trip, withCompanion, seek, baggageCount, maxWait } = draft;
  if (
    !trip ||
    withCompanion === undefined ||
    seek === undefined ||
    baggageCount === undefined ||
    maxWait === undefined
  ) {
    return null;
  }
  return {
    trip_id: trip.id,
    with_companion: withCompanion,
    baggage_count: baggageCount,
    max_wait_minutes: maxWait,
    seek_companions: seek,
  };
}
