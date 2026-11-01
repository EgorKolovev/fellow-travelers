import type { LucideIcon, LucideProps } from 'lucide-react';

interface Props extends Omit<LucideProps, 'ref'> {
  /** Иконка lucide (именованный импорт: `import { Plane } from 'lucide-react'`). */
  icon: LucideIcon;
}

/**
 * Линейная иконка в едином стиле: 20px, штрих 1.75, цвет — `currentColor` (наследуется от текста).
 * По умолчанию декоративная (`aria-hidden`): смысл передаёт текст или `aria-label` кнопки.
 */
export function Icon({
  icon: Glyph,
  size = 20,
  strokeWidth = 1.75,
  color = 'currentColor',
  'aria-hidden': ariaHidden = true,
  ...rest
}: Props) {
  return (
    <Glyph
      size={size}
      strokeWidth={strokeWidth}
      color={color}
      aria-hidden={ariaHidden}
      focusable="false"
      {...rest}
    />
  );
}
