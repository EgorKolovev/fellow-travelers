import { describe, expect, it } from 'vitest';
import type { Application } from '../../api/types';
import { makeTrip } from '../../test/fixtures';
import {
  applyCompanionChoice,
  companionChoice,
  draftFromApplication,
  draftToInput,
  joinWait,
  splitWait,
} from './draft';

const trip = makeTrip();

function makeApplication(overrides: Partial<Application> = {}): Application {
  return {
    id: 'app',
    event_id: trip.event_id,
    trip,
    with_companion: true,
    passengers: 2,
    baggage_count: 2,
    max_wait_minutes: 30,
    status: 'solo',
    at_meeting_point: false,
    departed_at: null,
    transfer_id: null,
    created_at: '2026-08-01T10:00:00+00:00',
    updated_at: '2026-08-01T10:00:00+00:00',
    ...overrides,
  };
}

describe('draft', () => {
  it('выбор попутчиков ↔ поля черновика', () => {
    expect(applyCompanionChoice({}, 'alone')).toEqual({ withCompanion: false, seek: true });
    expect(applyCompanionChoice({}, 'plusOne')).toEqual({ withCompanion: true, seek: true });
    expect(applyCompanionChoice({}, 'noSeek')).toEqual({ withCompanion: false, seek: false });
    expect(companionChoice({})).toBeUndefined();
    expect(companionChoice({ withCompanion: true, seek: false })).toBe('noSeek');
    expect(companionChoice({ withCompanion: true, seek: true })).toBe('plusOne');
  });

  it('из заявки: все поля; seek=false только для solo', () => {
    expect(draftFromApplication(makeApplication({ status: 'searching' }))).toEqual({
      trip,
      withCompanion: true,
      seek: true,
      baggageCount: 2,
      maxWait: 30,
    });
    expect(draftFromApplication(makeApplication()).seek).toBe(false);
    expect(draftFromApplication(makeApplication(), { seekCompanions: true }).seek).toBe(true);
    expect(draftFromApplication(makeApplication({ status: 'cancelled' })).seek).toBe(true);
    expect(draftFromApplication(makeApplication({ status: 'assigned' })).seek).toBe(true);
    expect(draftFromApplication(undefined)).toEqual({});
  });

  it('тело PUT только из полного черновика, seek_companions — явно', () => {
    expect(draftToInput({ trip, withCompanion: false, seek: true, baggageCount: 0 })).toBeNull();
    expect(
      draftToInput({ trip, withCompanion: false, seek: false, baggageCount: 0, maxWait: 0 }),
    ).toEqual({
      trip_id: trip.id,
      with_companion: false,
      baggage_count: 0,
      max_wait_minutes: 0,
      seek_companions: false,
    });
  });

  it('своё время ожидания: минуты → часы и минуты', () => {
    expect(splitWait(90)).toEqual({ hours: '1', minutes: '30' });
    expect(splitWait(45)).toEqual({ hours: '0', minutes: '45' });
    expect(splitWait(1440)).toEqual({ hours: '24', minutes: '0' });
    expect(draftFromApplication(makeApplication({ max_wait_minutes: 90 })).maxWait).toBe(90);
  });

  it.each([
    ['1', '30', 90],
    ['24', '0', 1440],
    ['24', '', 1440],
    ['', '45', 45],
    [' 2 ', '05', 125],
    ['0', '1', 1],
  ])('часы %j и минуты %j → %i', (hours, minutes, expected) => {
    expect(joinWait({ hours, minutes })).toBe(expected);
  });

  it.each([
    ['0', '0'],
    ['', ''],
    ['24', '1'],
    ['25', '0'],
    ['1', '60'],
    ['-1', '30'],
    ['1.5', '0'],
    ['1', 'abc'],
  ])('часы %j и минуты %j не подходят', (hours, minutes) => {
    expect(joinWait({ hours, minutes })).toBeNull();
  });
});
