import { ArrowRight } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';
import { isInviteCode } from '../../lib/inviteCode';
import { useBack } from '../../lib/useBack';
import { Button } from '../../ui/Button/Button';
import { Icon } from '../../ui/Icon/Icon';
import { Screen } from '../../ui/Screen/Screen';
import { ScreenHeader } from '../../ui/ScreenHeader/ScreenHeader';
import { TextInput } from '../../ui/TextInput/TextInput';
import styles from './Join.module.css';

const CODE_LENGTH = 6;
const ALLOWED_RE = /^[A-Z0-9]*$/;

/** Ввод кода приглашения → превью `/invite/:code`. */
export function Join() {
  const navigate = useNavigate();
  useBack('/');
  const [code, setCode] = useState('');
  const valid = isInviteCode(code);
  // Кириллица и знаки — частая ошибка при наборе: подсказываем сразу, а не молча блокируем кнопку.
  const error = ALLOWED_RE.test(code) ? undefined : 'Только латинские буквы и цифры';

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (valid) void navigate(`/invite/${code}`);
  }

  return (
    <Screen>
      <ScreenHeader
        title="Вступить в группу"
        subtitle="Введите код из приглашения — 6 букв и цифр"
      />
      <form onSubmit={onSubmit} noValidate>
        <div className={styles.form}>
          <TextInput
            label="Код приглашения"
            value={code}
            onChange={(value) => setCode(value.replace(/\s/g, '').toUpperCase())}
            placeholder="K7Q2ZP"
            maxLength={CODE_LENGTH}
            autoCapitalize="characters"
            autoComplete="off"
            error={error}
            name="code"
          />
        </div>
        <div className={styles.footer}>
          <Button type="submit" disabled={!valid} arrow={<Icon icon={ArrowRight} />}>
            Далее
          </Button>
        </div>
      </form>
    </Screen>
  );
}
