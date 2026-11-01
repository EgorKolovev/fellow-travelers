import { describe, expect, it } from 'vitest';
import { formatBaggage, formatFlightNumber, formatName, formatPhone, formatWait } from './format';

describe('formatPhone', () => {
  it('российский номер', () => {
    expect(formatPhone('+79122042912')).toBe('+7 912 204 29 12');
  });

  it('прочие номера как есть', () => {
    expect(formatPhone('+375291234567')).toBe('+375291234567');
    expect(formatPhone('+7912')).toBe('+7912');
  });
});

describe('formatName', () => {
  it('имя и фамилия', () => {
    expect(formatName('Иван', 'Петров')).toBe('Иван Петров');
  });

  it('без фамилии', () => {
    expect(formatName('Иван', null)).toBe('Иван');
  });
});

describe('formatFlightNumber', () => {
  it.each([
    ['SU1148', 'SU 1148'],
    ['U6115', 'U6 115'],
    ['5N531', '5N 531'],
    ['SU 1148', 'SU 1148'],
    ['ABCDEF', 'ABCDEF'],
  ])('%s → %s', (raw, expected) => {
    expect(formatFlightNumber(raw)).toBe(expected);
  });
});

describe('formatBaggage', () => {
  it.each([
    [0, 'только ручная кладь'],
    [1, '1 чемодан'],
    [2, '2 чемодана'],
    [5, '5 чемоданов'],
  ])('%i → %s', (n, expected) => {
    expect(formatBaggage(n)).toBe(expected);
  });
});

describe('formatWait', () => {
  it.each([
    [0, 'Не хочу ждать'],
    [15, '15 минут'],
    [30, '30 минут'],
    [1, '1 минута'],
    [45, '45 минут'],
    [60, '1 час'],
    [90, '1 ч 30 мин'],
    [120, '2 часа'],
    [300, '5 часов'],
    [1439, '23 ч 59 мин'],
    [1440, '24 часа'],
  ])('%i → %s', (n, expected) => {
    expect(formatWait(n)).toBe(expected);
  });
});
