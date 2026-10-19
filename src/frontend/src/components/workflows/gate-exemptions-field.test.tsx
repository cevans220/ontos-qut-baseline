/**
 * The designer field that switches a status gate off per domain, data product
 * or data contract (part of #1114).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('@/hooks/use-domains', () => ({
  useDomains: () => ({
    domains: [{ id: 'dom-sales', name: 'Sales' }, { id: 'dom-fin', name: 'Finance' }],
    loading: false,
    getDomainName: (id: string) => ({ 'dom-sales': 'Sales', 'dom-fin': 'Finance' } as Record<string, string>)[id],
  }),
}));

import i18n from '@/i18n/config';
import GateExemptionsField, { EMPTY_GATE_EXEMPTIONS, toOptions } from './gate-exemptions-field';

const lists: Record<string, unknown> = {
  '/api/data-products': [{ id: 'p-1', name: 'Orders' }, { id: 'p-2', name: 'Payments' }],
  '/api/data-contracts': { items: [{ id: 'c-1', name: 'Orders Contract' }] },
};

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn((url: string) =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(lists[url] ?? []) })));
});

afterEach(async () => {
  vi.unstubAllGlobals();
  await i18n.changeLanguage('en');
});

describe('toOptions', () => {
  it('reads a plain array or {items}, and keeps only rows with an id', () => {
    expect(toOptions([{ id: 'a', name: 'A' }, { name: 'no id' }])).toEqual([{ id: 'a', name: 'A' }]);
    expect(toOptions({ items: [{ id: 'b' }] })).toEqual([{ id: 'b', name: 'b' }]);
    expect(toOptions(null)).toEqual([]);
  });
});

describe('GateExemptionsField', () => {
  it('shows the selected entities by name and removes one', async () => {
    const onChange = vi.fn();
    render(
      <GateExemptionsField
        value={{ domains: ['dom-sales'], data_products: ['p-2'], data_contracts: [] }}
        onChange={onChange}
      />,
    );
    expect(screen.getByText('Switched off for')).toBeInTheDocument();
    expect(screen.getByTestId('gate-disabled-domains-selected')).toHaveTextContent('Sales');
    await waitFor(() =>
      expect(screen.getByTestId('gate-disabled-products-selected')).toHaveTextContent('Payments'));
    fireEvent.click(screen.getByRole('button', { name: 'Remove Sales' }));
    expect(onChange).toHaveBeenCalledWith({ domains: [], data_products: ['p-2'], data_contracts: [] });
  });

  it('adds a domain from the picker', async () => {
    const onChange = vi.fn();
    render(<GateExemptionsField value={EMPTY_GATE_EXEMPTIONS} onChange={onChange} />);
    fireEvent.click(screen.getByRole('combobox', { name: 'Domains' }));
    fireEvent.click(await screen.findByText('Finance'));
    expect(onChange).toHaveBeenCalledWith({ domains: ['dom-fin'], data_products: [], data_contracts: [] });
  });

  it('is translated', async () => {
    await i18n.changeLanguage('pt');
    render(<GateExemptionsField value={EMPTY_GATE_EXEMPTIONS} onChange={vi.fn()} />);
    expect(screen.getByText('Desligado para')).toBeInTheDocument();
    expect(screen.getByText('Produtos de dados')).toBeInTheDocument();
    expect(screen.getAllByText('Nenhum')).toHaveLength(3);
  });
});
