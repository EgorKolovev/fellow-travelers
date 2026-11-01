import {
  ArrowRight,
  Backpack,
  Luggage,
  Timer,
  User,
  Users,
  UserX,
  type LucideIcon,
} from 'lucide-react';
import { useMe } from '../../api/hooks';
import type { ApplicationInput, Trip } from '../../api/types';
import { formatBaggage, formatName, formatPhone, formatWait } from '../../lib/format';
import { getInitDataPhotoUrl } from '../../lib/telegram';
import { Avatar } from '../../ui/Avatar/Avatar';
import { Button } from '../../ui/Button/Button';
import { FlightInfo } from '../../ui/FlightInfo/FlightInfo';
import { Icon } from '../../ui/Icon/Icon';
import { Option } from '../../ui/Option/Option';
import { Screen } from '../../ui/Screen/Screen';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import styles from './ApplicationWizard.module.css';
import { WizardHeader } from './WizardHeader';

/** Шаг, на который ведёт карандаш строки сводки. */
export type ReviewSection = 'profile' | 'flight' | 'companion' | 'baggage' | 'wait';

/** Ошибка отправки: текст и, если дальше в мастере делать нечего, кнопка к группе. */
export interface SubmitError {
  message: string;
  action?: { label: string; onClick: () => void };
}

interface Props {
  trip: Trip;
  input: ApplicationInput;
  onEdit: (section: ReviewSection) => void;
  onSubmit: () => void;
  pending: boolean;
  error: SubmitError | null;
}

function companionRow(input: ApplicationInput): { icon: LucideIcon; title: string } {
  if (input.seek_companions === false) return { icon: UserX, title: 'Не ищу попутчиков' };
  return input.with_companion
    ? { icon: Users, title: 'Лечу с +1' }
    : { icon: User, title: 'Лечу один' };
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function ProfileRow({ onEdit }: { onEdit: () => void }) {
  const { data: me, isPending } = useMe();
  if (isPending) return <Skeleton />;
  if (!me) return null;
  const contacts = [me.username && `@${me.username}`, me.phone && formatPhone(me.phone)]
    .filter(Boolean)
    .join(' · ');
  const name = formatName(me.first_name, me.last_name);
  return (
    <Option
      leading={<Avatar src={me.photo_url ?? getInitDataPhotoUrl()} size={32} decorative />}
      title={name}
      subtitle={contacts || undefined}
      trailing="edit"
      onClick={onEdit}
      aria-label={`Изменить данные профиля: ${name}`}
    />
  );
}

/** Сводка «Всё верно?»: каждая строка с карандашом ведёт на свой шаг, «Да, всё верно» отправляет заявку. */
export function ReviewStep({ trip, input, onEdit, onSubmit, pending, error }: Props) {
  const companion = companionRow(input);
  const baggage = capitalize(formatBaggage(input.baggage_count));
  const wait =
    input.max_wait_minutes > 0
      ? `Готов ждать ${formatWait(input.max_wait_minutes)}`
      : formatWait(0);

  const row = (section: ReviewSection, icon: LucideIcon, title: string) => (
    <li>
      <Option
        leading={<Icon icon={icon} size={24} />}
        title={title}
        trailing="edit"
        onClick={() => onEdit(section)}
        aria-label={`Изменить: ${title}`}
      />
    </li>
  );

  return (
    <Screen>
      <WizardHeader step={5} title="Всё верно?" subtitle="Проверьте данные" />
      <ul className={styles.options} aria-busy={pending || undefined}>
        <li>
          <ProfileRow onEdit={() => onEdit('profile')} />
        </li>
        <li>
          <FlightInfo trip={trip} onEdit={() => onEdit('flight')} editLabel="Изменить рейс" />
        </li>
        {row('companion', companion.icon, companion.title)}
        {row('baggage', input.baggage_count > 0 ? Luggage : Backpack, baggage)}
        {row('wait', Timer, wait)}
      </ul>
      <div className={styles.footer}>
        <Button arrow={<Icon icon={ArrowRight} />} onClick={onSubmit} disabled={pending}>
          Да, всё верно
        </Button>
        {error && (
          <>
            <p className={styles.notice} role="alert">
              {error.message}
            </p>
            {error.action && (
              <Button variant="secondary" onClick={error.action.onClick}>
                {error.action.label}
              </Button>
            )}
          </>
        )}
      </div>
    </Screen>
  );
}
