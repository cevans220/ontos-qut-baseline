import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import { GuidedGenerateDialog, composeGuidedPrompt } from './guided-generate-dialog';

const mockUseDomains = vi.fn();
vi.mock('@/hooks/use-domains', () => ({
  useDomains: () => mockUseDomains(),
}));

const DOMAINS = [
  { id: 'd1', name: 'Finance', description: null },
  { id: 'd2', name: 'Logistics', description: null },
];

// Radix Select relies on pointer-capture APIs that jsdom does not implement.
beforeAll(() => {
  const proto = window.HTMLElement.prototype as any;
  proto.hasPointerCapture ??= () => false;
  proto.releasePointerCapture ??= () => {};
  proto.setPointerCapture ??= () => {};
});

function GeneratorProbe() {
  const loc = useLocation();
  return <div data-testid="generator-search">{loc.search}</div>;
}

function renderDialog() {
  return render(
    <MemoryRouter initialEntries={['/concepts']}>
      <Routes>
        <Route path="/concepts" element={<GuidedGenerateDialog open onOpenChange={vi.fn()} />} />
        <Route path="/concepts/generator" element={<GeneratorProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('composeGuidedPrompt', () => {
  it('numbers non-blank questions and skips blanks', () => {
    const prompt = composeGuidedPrompt('Finance', ['Q one?', '  ', 'Q two?'], 'Invoice');
    expect(prompt).toBe(
      'Build a domain ontology for: Finance.\n' +
        'It should help answer these questions:\n1. Q one?\n2. Q two?\n' +
        'Key objects and concepts to model: Invoice.',
    );
  });

  it('omits the questions part when all questions are blank', () => {
    expect(composeGuidedPrompt('Finance', ['', ' '], '')).toBe('Build a domain ontology for: Finance.');
  });
});

describe('GuidedGenerateDialog', () => {
  beforeEach(() => {
    mockUseDomains.mockReturnValue({ domains: DOMAINS, loading: false, error: null });
  });

  it('renders real data domains in the dropdown and fills the domain field', async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole('combobox', { name: 'Data domain' }));
    const listbox = await screen.findByRole('listbox');
    expect(within(listbox).getByRole('option', { name: 'Finance' })).toBeInTheDocument();
    expect(within(listbox).getByRole('option', { name: 'Logistics' })).toBeInTheDocument();
    expect(within(listbox).getByRole('option', { name: /No data domain/ })).toBeInTheDocument();
    // Hardcoded example chips are gone.
    expect(screen.queryByText('Supply chain')).not.toBeInTheDocument();

    await user.click(within(listbox).getByRole('option', { name: 'Logistics' }));
    expect(screen.getByLabelText('What domain are you working in?')).toHaveValue('Logistics');
  });

  it('shows a loading state while domains load', () => {
    mockUseDomains.mockReturnValue({ domains: [], loading: true, error: null });
    renderDialog();
    expect(screen.getByTestId('guided-domains-loading')).toBeInTheDocument();
  });

  it('shows a hint and keeps free text usable when no domains exist', async () => {
    mockUseDomains.mockReturnValue({ domains: [], loading: false, error: null });
    const user = userEvent.setup();
    renderDialog();
    expect(screen.getByTestId('guided-domains-empty')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Data domain' })).not.toBeInTheDocument();

    await user.type(screen.getByLabelText('What domain are you working in?'), 'Clinical trials');
    expect(screen.getByTestId('guided-prompt-preview')).toHaveTextContent(
      'Build a domain ontology for: Clinical trials.',
    );
  });

  it('composes multiple questions (Enter adds, blanks ignored, remove works) into the prompt', async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.type(screen.getByLabelText('What domain are you working in?'), 'Finance');
    await user.type(screen.getByLabelText('Question 1'), 'Which invoices are overdue?{Enter}');
    // Enter in the last input added and focused a new one.
    expect(screen.getByLabelText('Question 2')).toHaveFocus();
    await user.keyboard('Temp question{Enter}');
    await user.type(screen.getByLabelText('Question 3'), 'Who owns each cost center?');
    await user.click(screen.getByRole('button', { name: 'Add question' }));
    // Question 4 stays blank and must be ignored. Remove question 2.
    await user.click(screen.getByRole('button', { name: 'Remove question 2' }));

    const preview = screen.getByTestId('guided-prompt-preview');
    expect(preview.textContent).toBe(
      'Build a domain ontology for: Finance.\n' +
        'It should help answer these questions:\n' +
        '1. Which invoices are overdue?\n2. Who owns each cost center?',
    );

    await user.click(screen.getByRole('button', { name: 'Continue to generator' }));
    const search = screen.getByTestId('generator-search').textContent ?? '';
    const guidelines = new URLSearchParams(search).get('guidelines');
    expect(guidelines).toBe(preview.textContent);
  });
});
