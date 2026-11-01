import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Location } from '../../api/types';
import { GDZ, SVO } from '../../test/fixtures';
import { LocationPicker } from './LocationPicker';

const searchLocations = vi.hoisted(() => vi.fn());

vi.mock('../../api/locations', () => ({ searchLocations }));

function Harness({ initial = null }: { initial?: Location | null }) {
  const [value, setValue] = useState<Location | null>(initial);
  return (
    <>
      <LocationPicker label="Аэропорт вылета" value={value} onChange={setValue} />
      <p data-testid="value">{value?.code ?? 'нет'}</p>
    </>
  );
}

function renderPicker(initial?: Location | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness initial={initial} />
    </QueryClientProvider>,
  );
}

/** Прокрутить фейковое время и дать отработать промисам запроса. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Ответ пришёл — дать TanStack Query отрисовать результат (его уведомления тоже на таймерах). */
const flush = () => advance(50);

describe('LocationPicker', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    searchLocations.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('запрос — только от 2 символов и через 300 мс после ввода', async () => {
    searchLocations.mockResolvedValue([SVO]);
    renderPicker();
    const input = screen.getByLabelText('Аэропорт вылета');

    fireEvent.change(input, { target: { value: 'м' } });
    await advance(500);
    expect(searchLocations).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'мо' } });
    await advance(200);
    fireEvent.change(input, { target: { value: 'мос' } });
    await advance(299);
    expect(searchLocations).not.toHaveBeenCalled();

    await advance(1);
    expect(searchLocations).toHaveBeenCalledTimes(1);
    expect(searchLocations).toHaveBeenCalledWith('мос');
    await flush();
    expect(screen.getByRole('button', { name: /Москва · SVO/ })).toHaveTextContent('Шереметьево');
  });

  it('выбор → карточка с карандашом; «изменить» возвращает поиск', async () => {
    searchLocations.mockResolvedValue([SVO, GDZ]);
    renderPicker();

    fireEvent.change(screen.getByLabelText('Аэропорт вылета'), { target: { value: 'SV' } });
    await advance(300);
    await flush();
    fireEvent.click(screen.getByRole('button', { name: /Москва · SVO/ }));
    expect(screen.getByTestId('value')).toHaveTextContent('SVO');

    const selected = screen.getByRole('button', {
      name: /Аэропорт вылета: Москва · SVO.*Изменить/,
    });
    expect(selected.querySelector('.lucide-pencil')).not.toBeNull();
    fireEvent.click(selected);
    expect(screen.getByTestId('value')).toHaveTextContent('нет');
    expect(screen.getByLabelText('Аэропорт вылета')).toHaveValue('');
  });

  it('пусто — «Ничего не нашли»', async () => {
    searchLocations.mockResolvedValue([]);
    renderPicker();
    fireEvent.change(screen.getByLabelText('Аэропорт вылета'), { target: { value: 'zzz' } });
    await advance(300);
    await flush();
    expect(screen.getByRole('status')).toHaveTextContent('Ничего не нашли');
  });
});
