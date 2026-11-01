import { PlaneTakeoff } from 'lucide-react';
import { useState } from 'react';
import { LOCATIONS_MIN_QUERY, useLocations } from '../../api/hooks';
import type { Location } from '../../api/types';
import { useDebounced } from '../../lib/useDebounced';
import fieldStyles from '../../ui/Field/Field.module.css';
import { Icon } from '../../ui/Icon/Icon';
import { Option } from '../../ui/Option/Option';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { TextInput } from '../../ui/TextInput/TextInput';
import styles from './LocationPicker.module.css';

/** Задержка перед запросом, пока печатают. */
export const LOCATION_SEARCH_DEBOUNCE_MS = 300;

interface Props {
  label: string;
  value: Location | null;
  onChange: (location: Location | null) => void;
  error?: string;
}

function locationTitle(location: Location): string {
  return `${location.city} · ${location.code}`;
}

/** Выбор аэропорта: поиск по городу, названию или коду; выбранный — карточка с карандашом. */
export function LocationPicker({ label, value, onChange, error }: Props) {
  const [query, setQuery] = useState('');
  const debounced = useDebounced(query.trim(), LOCATION_SEARCH_DEBOUNCE_MS);
  const searching = !value && debounced.length >= LOCATIONS_MIN_QUERY;
  const locations = useLocations(searching ? debounced : '');

  if (value) {
    return (
      <div className={fieldStyles.field}>
        <p className={fieldStyles.label}>{label}</p>
        <Option
          leading={<Icon icon={PlaneTakeoff} size={24} />}
          title={locationTitle(value)}
          subtitle={value.name}
          trailing="edit"
          aria-label={`${label}: ${locationTitle(value)}, ${value.name}. ${error ? `${error}. ` : ''}Изменить`}
          onClick={() => {
            setQuery('');
            onChange(null);
          }}
        />
        {error && <p className={styles.error}>{error}</p>}
      </div>
    );
  }

  let results = null;
  if (searching) {
    if (locations.isPending) {
      results = (
        <div aria-busy="true">
          <Skeleton count={2} />
        </div>
      );
    } else if (locations.error) {
      results = (
        <p className={styles.message} role="alert">
          {locations.error.message}
        </p>
      );
    } else if (locations.data.length === 0) {
      results = (
        <p className={styles.message} role="status">
          Ничего не нашли
        </p>
      );
    } else {
      results = (
        <ul className={styles.list} aria-label={`${label}: результаты поиска`}>
          {locations.data.map((location) => (
            <li key={location.id}>
              <Option
                title={locationTitle(location)}
                subtitle={location.name}
                onClick={() => onChange(location)}
              />
            </li>
          ))}
        </ul>
      );
    }
  }

  return (
    <div className={styles.picker}>
      <TextInput
        label={label}
        type="search"
        value={query}
        onChange={setQuery}
        placeholder="Город, аэропорт или код"
        hint={searching ? undefined : 'Введите хотя бы 2 буквы'}
        autoComplete="off"
        error={error}
      />
      {results}
    </div>
  );
}
