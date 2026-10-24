/**
 * Run detail page: polls the run only while it is in flight (one request at a
 * time), loads suggestions once it completed and groups them by table, and
 * offers cancel while active / delete once finished.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import MetadataGenerationRunDetails from './metadata-generation-run-details';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));

vi.mock('@/hooks/use-api', () => ({ useApi: () => api }));
vi.mock('@/hooks/use-toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('@/stores/permissions-store', () => ({
  usePermissions: () => ({ hasPermission: () => true }),
}));
vi.mock('@/stores/breadcrumb-store', () => ({ default: () => () => undefined }));

const run = (status: string) => ({
  run_id: 'run-1', user_id: 'a@x.com', status, selection: [{ type: 'schema', fqn: 'main.sales' }],
  options: { comment_style: 'standard', model: 'm' }, suggestion_count: 2,
  created_at: '2026-10-24T10:00:00Z', updated_at: '2026-10-24T10:00:00Z',
  counts: status === 'completed' ? { pending: 2 } : {},
});

const SUGGESTIONS = [
  { id: 's1', run_id: 'run-1', kind: 'description', object_type: 'table', table_fqn: 'main.sales.orders',
    column_name: null, base_text: null, proposed_text: 'Customer orders.', status: 'pending' },
  { id: 's2', run_id: 'run-1', kind: 'description', object_type: 'column', table_fqn: 'main.sales.customers',
    column_name: 'email', base_text: 'Mail', proposed_text: 'Customer email address.', status: 'pending' },
];

function Probe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/metadata-generation/runs/run-1']}>
      <Routes>
        <Route path="/metadata-generation/runs/:runId" element={<MetadataGenerationRunDetails />} />
        <Route path="/metadata-generation" element={<Probe />} />
      </Routes>
    </MemoryRouter>,
  );

const runCalls = () => api.get.mock.calls.filter(([url]) => url === '/api/metadata-generation/runs/run-1').length;

describe('MetadataGenerationRunDetails', () => {
  beforeEach(() => {
    api.get.mockReset();
    api.post.mockReset();
    api.delete.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('polls while running, stops once completed, then loads suggestions', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const statuses = ['running', 'ingesting', 'completed'];
    api.get.mockImplementation(async (url: string) =>
      url.endsWith('/suggestions')
        ? { data: { suggestions: SUGGESTIONS, total: 2 } }
        : { data: run(statuses.shift() ?? 'completed') },
    );
    renderPage();

    expect(await screen.findByText('Cancel run')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(await screen.findByText('main.sales.orders')).toBeInTheDocument();
    expect(runCalls()).toBe(3);

    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(runCalls()).toBe(3); // no polling once terminal
  });

  it('groups suggestions under their table and shows current vs proposed', async () => {
    api.get.mockImplementation(async (url: string) =>
      url.endsWith('/suggestions') ? { data: { suggestions: SUGGESTIONS, total: 2 } } : { data: run('completed') },
    );
    renderPage();

    expect(await screen.findByText('main.sales.orders')).toBeInTheDocument();
    expect(screen.getByText('main.sales.customers')).toBeInTheDocument();
    expect(screen.getByText('Customer orders.')).toBeInTheDocument();
    expect(screen.getByText('Mail')).toBeInTheDocument();
    expect(screen.getByText('Customer email address.')).toBeInTheDocument();
    expect(screen.queryByText('Cancel run')).not.toBeInTheDocument();
  });

  it('deletes a finished run and returns to the list', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.get.mockImplementation(async (url: string) =>
      url.endsWith('/suggestions') ? { data: { suggestions: [], total: 0 } } : { data: run('failed') },
    );
    api.delete.mockResolvedValue({ data: null });
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: /Delete run/ }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/metadata-generation/runs/run-1'));
    expect(await screen.findByTestId('location')).toHaveTextContent('/metadata-generation');
  });
});
