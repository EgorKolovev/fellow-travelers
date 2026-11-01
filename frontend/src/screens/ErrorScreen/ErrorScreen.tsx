import { ArrowRight } from 'lucide-react';
import { Button } from '../../ui/Button/Button';
import { EmptyState } from '../../ui/EmptyState/EmptyState';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import styles from './ErrorScreen.module.css';

interface Props {
  title?: string;
  text: string;
  onRetry?: () => void;
  /** Своё действие вместо «Повторить попытку» (например «Мои группы»). */
  action?: { label: string; onClick: () => void };
}

/** Карточка ошибки без экрана — для блока внутри экрана (вкладка): иконка, текст и действие. */
export function ErrorCard({
  title = 'Не удалось загрузить страницу',
  text,
  onRetry,
  action,
}: Props) {
  const button = action ?? (onRetry && { label: 'Повторить попытку', onClick: onRetry });
  return (
    <>
      <div className={styles.box} role="alert">
        <EmptyState title={title} text={text} />
      </div>
      {button && (
        <div className={styles.footer}>
          <Button onClick={button.onClick} arrow={<Icon icon={ArrowRight} />}>
            {button.label}
          </Button>
        </div>
      )}
    </>
  );
}

/** Страница ошибки: карточка с иконкой, заголовком и текстом, «Повторить попытку». */
export function ErrorScreen(props: Props) {
  return (
    <Screen center>
      <ErrorCard {...props} />
    </Screen>
  );
}

export function OpenInTelegramScreen() {
  return (
    <ErrorScreen
      title="Откройте приложение из Telegram"
      text="Это мини-приложение работает только внутри Telegram. Откройте его через нашего бота."
    />
  );
}
