import { UserRound } from 'lucide-react';
import { useState } from 'react';
import { isSafeExternalUrl } from '../../lib/links';
import { Icon } from '../Icon/Icon';
import styles from './Avatar.module.css';

interface Props {
  src?: string | null;
  loading?: boolean;
  /** Диаметр в px; по умолчанию 128 (экран профиля), в списках — 32. */
  size?: number;
  /** Декоративный (рядом есть имя): скрыт от скринридера, без подписи «Нет фото». */
  decorative?: boolean;
}

export function Avatar({ src, loading = false, size = 128, decorative = false }: Props) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  // Фото — только по https (photo_url приходит извне); иначе заглушка.
  const showImage = isSafeExternalUrl(src) && src !== failedSrc;

  return (
    <div
      className={`${styles.avatar} ${loading ? styles.loading : ''}`}
      style={size === 128 ? undefined : { width: size, height: size }}
      aria-hidden={decorative || undefined}
    >
      {showImage ? (
        <img
          className={styles.image}
          src={src}
          alt=""
          referrerPolicy="no-referrer"
          onError={() => setFailedSrc(src)}
        />
      ) : (
        !loading && (
          <span
            className={styles.placeholder}
            role={decorative ? undefined : 'img'}
            aria-label={decorative ? undefined : 'Нет фото'}
          >
            <Icon icon={UserRound} size={size / 2} strokeWidth={size >= 64 ? 1.25 : 1.75} />
          </span>
        )
      )}
    </div>
  );
}
