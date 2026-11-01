import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { makeContact } from '../../test/fixtures';
import { FellowList } from './FellowList';

vi.mock('../../lib/links', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/links')>()),
  openExternal: vi.fn(() => true),
}));

describe('FellowList', () => {
  it('с username и телефоном — обе ссылки и контакты текстом', () => {
    render(
      <FellowList
        items={[{ id: '1', contact: makeContact(), passengers: 2, caption: 'Летит тем же рейсом' }]}
      />,
    );
    const chat = screen.getByRole('link', { name: 'Написать в Telegram: Луна Сокол' });
    expect(chat).toHaveAttribute('href', 'https://t.me/luna_sokol');
    expect(chat).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByRole('link', { name: 'Позвонить: Луна Сокол' })).toHaveAttribute(
      'href',
      'tel:+79122042912',
    );
    expect(screen.getByText('+7 912 204 29 12 · @luna_sokol')).toBeInTheDocument();
    expect(screen.getByText('Летит тем же рейсом')).toBeInTheDocument();
    expect(screen.getByText('+1')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText(', со спутником')).toBeInTheDocument();
    // Аватар декоративный: без «Нет фото» для скринридера
    expect(screen.queryByRole('img', { name: 'Нет фото' })).not.toBeInTheDocument();
  });

  it('без username — нет «Написать», без телефона — нет «Позвонить»', () => {
    render(
      <FellowList
        items={[
          { id: '1', contact: makeContact({ username: null }) },
          {
            id: '2',
            contact: makeContact({
              user_id: 'x',
              first_name: 'Антон',
              last_name: null,
              phone: null,
            }),
          },
        ]}
      />,
    );
    const [first, second] = screen.getAllByRole('listitem');
    expect(within(first!).queryByRole('link', { name: /Telegram/ })).not.toBeInTheDocument();
    expect(within(first!).getByRole('link', { name: /Позвонить/ })).toBeInTheDocument();
    expect(within(second!).getByRole('link', { name: /Telegram/ })).toBeInTheDocument();
    expect(within(second!).queryByRole('link', { name: /Позвонить/ })).not.toBeInTheDocument();
    expect(screen.queryByText('+1')).not.toBeInTheDocument();
  });

  it('невалидный username не превращается в ссылку', () => {
    render(
      <FellowList
        items={[{ id: '1', contact: makeContact({ username: 'bad/../x', phone: null }) }]}
      />,
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('photo_url не по https — заглушка вместо <img>', () => {
    const { container } = render(
      <FellowList
        items={[{ id: '1', contact: makeContact({ photo_url: 'javascript:alert(1)' }) }]}
      />,
    );
    expect(container.querySelector('img')).toBeNull();
  });

  it('showContacts=false — без контактов (для себя)', () => {
    render(
      <FellowList
        items={[{ id: '1', contact: makeContact(), caption: 'Это вы', showContacts: false }]}
      />,
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByText(/@luna_sokol/)).not.toBeInTheDocument();
  });
});
