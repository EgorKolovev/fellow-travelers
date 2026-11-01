import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Backpack } from 'lucide-react';
import { describe, expect, it, vi } from 'vitest';
import { Icon } from '../Icon/Icon';
import { Option } from './Option';

describe('Option', () => {
  it('с onClick — кнопка, клик вызывает обработчик', async () => {
    const onClick = vi.fn();
    render(
      <Option
        leading={<Icon icon={Backpack} size={24} />}
        title="Только ручная кладь"
        subtitle="Сразу выйду"
        onClick={onClick}
      />,
    );
    const button = screen.getByRole('button', { name: /Только ручная кладь/ });
    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it('без onClick — не кнопка и без стрелки по умолчанию', () => {
    const { container } = render(<Option title="Лечу один" />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByText('Лечу один')).toBeInTheDocument();
    expect(container.querySelector('svg')).toBeNull();
  });

  it('с onClick — стрелка по умолчанию', () => {
    render(<Option title="Москва" onClick={() => {}} />);
    const trailing = screen.getByRole('button', { name: 'Москва' }).querySelector('svg');
    expect(trailing).toHaveClass('lucide-chevron-right');
    expect(trailing).toHaveAttribute('aria-hidden', 'true');
  });

  it('disabled — клик не срабатывает', async () => {
    const onClick = vi.fn();
    render(<Option title="Москва" onClick={onClick} disabled />);
    const button = screen.getByRole('button', { name: /Москва/ });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });
});
