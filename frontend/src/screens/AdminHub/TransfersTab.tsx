import { CarTaxiFront } from 'lucide-react';
import { useAdminTransfers } from '../../api/hooks';
import type { Transfer } from '../../api/types';
import { plural } from '../../lib/datetime';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { Icon } from '../../ui/Icon/Icon';
import { Option } from '../../ui/Option/Option';
import { Skeleton } from '../../ui/Skeleton/Skeleton';
import { sortTransfers, transferNumbers, transferState, type TransferState } from './admin';
import styles from './AdminTabs.module.css';
import { TabError } from './TabError';

const PEOPLE = ['человек', 'человека', 'человек'] as const;

/** Подпись статуса трансфера в списке и её цвет. */
const TRANSFER_STATE_LABEL: Record<TransferState, { text: string; tone: string }> = {
  departed: { text: 'Уехали', tone: styles.soft ?? '' },
  ready: { text: 'Все на месте', tone: styles.success ?? '' },
  gathering: { text: 'Собираются', tone: styles.accent ?? '' },
};

function TransferCard({
  transfer,
  number,
  onOpen,
}: {
  transfer: Transfer;
  number: number;
  onOpen: () => void;
}) {
  const state = TRANSFER_STATE_LABEL[transferState(transfer)];
  const names = transfer.members.map((m) => m.contact.first_name).join(', ');
  return (
    <Option
      leading={<Icon icon={CarTaxiFront} size={24} />}
      title={`Трансфер ${number} · ${transfer.passengers_total} ${plural(transfer.passengers_total, PEOPLE)}`}
      subtitle={
        <>
          {names}
          <span className={`${styles.state} ${state.tone}`}>
            <span aria-hidden="true">● </span>
            {state.text}
          </span>
        </>
      }
      onClick={onOpen}
    />
  );
}

interface Props {
  eventId: string;
  onOpenTransfer: (transferId: string) => void;
}

/** Вкладка «Трансферы»: число трансферов и людей, карточки со статусом сбора. */
export function TransfersTab({ eventId, onOpenTransfer }: Props) {
  const { data, error, isPending, refetch } = useAdminTransfers(eventId);

  if (isPending) {
    return (
      <div className={styles.tab} aria-busy="true">
        <Skeleton height={16} />
        <Skeleton height={88} count={3} />
      </div>
    );
  }
  if (error) {
    return (
      <div className={styles.tab}>
        <TabError error={error} onRetry={() => void refetch()} />
      </div>
    );
  }
  if (data.length === 0) {
    return (
      <div className={styles.tab}>
        <EmptyState
          title="Трансферов пока нет"
          text="Они появятся, когда сервис подберёт попутчиков"
        />
      </div>
    );
  }

  const numbers = transferNumbers(data);
  const people = data.reduce((sum, t) => sum + t.passengers_total, 0);
  return (
    <div className={styles.tab}>
      <p className={styles.meta}>
        {data.length} {plural(data.length, ['трансфер', 'трансфера', 'трансферов'])} · {people}{' '}
        {plural(people, PEOPLE)}
      </p>
      <ul className={styles.list} aria-label="Трансферы">
        {sortTransfers(data).map((transfer) => (
          <li key={transfer.id}>
            <TransferCard
              transfer={transfer}
              number={numbers.get(transfer.id) ?? 0}
              onOpen={() => onOpenTransfer(transfer.id)}
            />
          </li>
        ))}
      </ul>
    </div>
  );
}
