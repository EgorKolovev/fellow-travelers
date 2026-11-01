import {
  ArrowLeft,
  ArrowRight,
  Backpack,
  Luggage,
  User,
  Users,
  UserX,
  type LucideIcon,
} from 'lucide-react';
import { useState } from 'react';
import type { WaitMinutes } from '../../api/types';
import { formatWait } from '../../lib/format';
import { Button } from '../../ui/Button/Button';
import { Form, FormFields, FormFooter } from '../../ui/Form/Form';
import { Icon } from '../../ui/Icon/Icon';
import { NumberInput } from '../../ui/NumberInput/NumberInput';
import { Option } from '../../ui/Option/Option';
import { Screen } from '../../ui/Screen/Screen';
import { TextInput } from '../../ui/TextInput/TextInput';
import styles from './ApplicationWizard.module.css';
import {
  joinWait,
  MAX_BAGGAGE,
  splitWait,
  WAIT_PRESETS,
  type CompanionChoice,
  type WaitParts,
} from './draft';
import { WizardHeader } from './WizardHeader';

const COMPANION_OPTIONS: {
  choice: CompanionChoice;
  icon: LucideIcon;
  title: string;
  subtitle?: string;
}[] = [
  { choice: 'alone', icon: User, title: 'Нет, лечу один' },
  { choice: 'plusOne', icon: Users, title: 'Да, со мной +1' },
  {
    choice: 'noSeek',
    icon: UserX,
    title: 'Не искать попутчиков',
    subtitle: 'Если вместе с вами летит больше 2 человек',
  },
];

/** Шаг 3 «Летите с кем-то?». */
export function CompanionStep({ onChoose }: { onChoose: (choice: CompanionChoice) => void }) {
  return (
    <Screen>
      <WizardHeader
        step={3}
        title="Летите с кем-то?"
        subtitle="Учтём при подборе попутчиков, чтобы все влезли в такси"
      />
      <ul className={styles.options}>
        {COMPANION_OPTIONS.map(({ choice, icon, title, subtitle }) => (
          <li key={choice}>
            <Option
              leading={<Icon icon={icon} size={24} />}
              title={title}
              subtitle={subtitle}
              onClick={() => onChoose(choice)}
            />
          </li>
        ))}
      </ul>
    </Screen>
  );
}

const BAGGAGE_SUBTITLE = 'Учтём при подборе попутчиков, чтобы всё влезло в такси';

/** Шаг 4 «Багаж»: ручная кладь или с багажом (дальше — счётчик). */
export function BaggageStep({
  onHandLuggage,
  onWithBaggage,
}: {
  onHandLuggage: () => void;
  onWithBaggage: () => void;
}) {
  return (
    <Screen>
      <WizardHeader step={4} title="Багаж" subtitle={BAGGAGE_SUBTITLE} />
      <ul className={styles.options}>
        <li>
          <Option
            leading={<Icon icon={Backpack} size={24} />}
            title="Только ручная кладь"
            subtitle="Сразу выйду из зоны прилёта"
            onClick={onHandLuggage}
          />
        </li>
        <li>
          <Option
            leading={<Icon icon={Luggage} size={24} />}
            title="Лечу с багажом"
            subtitle="Буду ждать выдачу багажа"
            onClick={onWithBaggage}
          />
        </li>
      </ul>
    </Screen>
  );
}

interface BaggageCountProps {
  initial: number;
  /** Летит со спутником: багаж считается на двоих. */
  withCompanion: boolean;
  /** Показывать подсказку «Не искать попутчиков» (если попутчиков и так не ищем — не нужна). */
  seeking: boolean;
  onNext: (count: number) => void;
  /** Больше двух чемоданов: не искать попутчиков и сохранить выбранное число. */
  onNoSeek: (count: number) => void;
  onBack: () => void;
}

/** Шаг 4, счётчик «Сколько чемоданов берёте?» (1..2). */
export function BaggageCountStep({
  initial,
  withCompanion,
  seeking,
  onNext,
  onNoSeek,
  onBack,
}: BaggageCountProps) {
  const [count, setCount] = useState(Math.min(Math.max(initial, 1), MAX_BAGGAGE));
  return (
    <Screen>
      <WizardHeader step={4} title="Багаж" subtitle={BAGGAGE_SUBTITLE} />
      <div className={styles.content}>
        <NumberInput
          label="Сколько чемоданов берёте?"
          value={count}
          onChange={setCount}
          min={1}
          max={MAX_BAGGAGE}
          hint={withCompanion ? 'Багаж считается на вас обоих' : undefined}
        />
        {seeking && (
          <>
            <p className={styles.hint}>
              В одно такси помещается не больше двух чемоданов. Если у вас больше — выберите «Не
              искать попутчиков»
            </p>
            <Button
              variant="secondary"
              icon={<Icon icon={UserX} />}
              onClick={() => onNoSeek(count)}
            >
              Не искать попутчиков
            </Button>
          </>
        )}
      </div>
      <div className={styles.footer}>
        <Button arrow={<Icon icon={ArrowRight} />} onClick={() => onNext(count)}>
          Далее
        </Button>
        <Button variant="secondary" icon={<Icon icon={ArrowLeft} />} onClick={onBack}>
          Назад
        </Button>
      </div>
    </Screen>
  );
}

const WAIT_ERROR = 'От 1 минуты до 24 часов';

interface WaitStepProps {
  /** Текущее значение при правке: не из готовых вариантов — сразу открыто «Другое время». */
  value?: WaitMinutes;
  onChoose: (minutes: WaitMinutes) => void;
}

/** Шаг 5 «Сколько готовы ждать попутчиков?»: 0 / 15 / 30 или своё время до 24 часов. */
export function WaitStep({ value, onChoose }: WaitStepProps) {
  const isCustom = value !== undefined && !WAIT_PRESETS.includes(value);
  const [custom, setCustom] = useState(isCustom);
  const [parts, setParts] = useState<WaitParts>(
    isCustom ? splitWait(value) : { hours: '', minutes: '' },
  );
  const [error, setError] = useState<string | null>(null);

  function change(next: WaitParts) {
    setParts(next);
    setError(null);
  }

  function submit() {
    const minutes = joinWait(parts);
    if (minutes === null) setError(WAIT_ERROR);
    else onChoose(minutes);
  }

  return (
    <Screen>
      <WizardHeader
        step={5}
        title="Сколько готовы ждать попутчиков?"
        subtitle="Будем искать по времени прилёта по расписанию. В одно такси посадим не больше трёх пассажиров"
      />
      <ul className={styles.options}>
        {WAIT_PRESETS.map((minutes) => (
          <li key={minutes}>
            <Option title={formatWait(minutes)} onClick={() => onChoose(minutes)} />
          </li>
        ))}
        <li>
          {/* Карточка остаётся на месте после нажатия, чтобы не терять фокус. */}
          <Option
            title="Другое время"
            subtitle="До 24 часов"
            trailing={custom ? 'none' : 'arrow'}
            onClick={() => setCustom(true)}
          />
        </li>
      </ul>
      {custom && (
        <Form onSubmit={submit}>
          <FormFields>
            <div className={styles.waitFields}>
              <TextInput
                label="Часы"
                value={parts.hours}
                onChange={(hours) => change({ ...parts, hours })}
                placeholder="0"
                inputMode="numeric"
                autoComplete="off"
                maxLength={2}
              />
              <TextInput
                label="Минуты"
                value={parts.minutes}
                onChange={(minutes) => change({ ...parts, minutes })}
                placeholder="0"
                inputMode="numeric"
                autoComplete="off"
                maxLength={2}
              />
            </div>
            {error && (
              <p className={styles.notice} role="alert">
                {error}
              </p>
            )}
          </FormFields>
          <FormFooter>
            <Button type="submit" arrow={<Icon icon={ArrowRight} />}>
              Далее
            </Button>
          </FormFooter>
        </Form>
      )}
    </Screen>
  );
}
