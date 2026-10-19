import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, defaultValue: string) => defaultValue || key,
    i18n: { language: 'en', changeLanguage: vi.fn() },
  }),
}));

import { RequestConceptActionMenu } from './request-concept-action-menu';

const submitReview = {
  status: 'under_review',
  action: 'submit-review',
  labelKey: 'semantic-models:lifecycle.submitReview',
  defaultLabel: 'Submit for review',
};
const approve = {
  status: 'approved',
  action: 'approve',
  labelKey: 'semantic-models:lifecycle.approve',
  defaultLabel: 'Approve',
};
const requestChanges = {
  status: 'draft',
  action: 'request-changes',
  labelKey: 'semantic-models:lifecycle.requestChanges',
  defaultLabel: 'Request changes',
};

async function openMenu() {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: /request/i }));
  return user;
}

describe('RequestConceptActionMenu', () => {
  it('renders nothing when there are no transitions and no new-version option', () => {
    const { container } = render(
      <RequestConceptActionMenu transitions={[]} onTransition={vi.fn()} newVersion={null} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('labels the draft submit-review step as "Request review" and runs it', async () => {
    const onTransition = vi.fn();
    render(<RequestConceptActionMenu transitions={[submitReview]} onTransition={onTransition} />);
    const user = await openMenu();
    const item = await screen.findByRole('menuitem', { name: 'Request review' });
    await user.click(item);
    expect(onTransition).toHaveBeenCalledWith('under_review', 'submit-review');
  });

  it('offers reviewer decisions and New version, consolidated in one menu', async () => {
    const onTransition = vi.fn();
    const onNewVersion = vi.fn();
    render(
      <RequestConceptActionMenu
        transitions={[approve, requestChanges]}
        onTransition={onTransition}
        newVersion={{ disabled: false, onSelect: onNewVersion }}
      />,
    );
    const user = await openMenu();
    expect(await screen.findByRole('menuitem', { name: 'Approve' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Request changes' })).toBeInTheDocument();
    await user.click(screen.getByRole('menuitem', { name: 'New version' }));
    expect(onNewVersion).toHaveBeenCalledTimes(1);
    expect(onTransition).not.toHaveBeenCalled();
  });

  it('keeps New version visible but disabled with its reason while locked', async () => {
    const onNewVersion = vi.fn();
    render(
      <RequestConceptActionMenu
        transitions={[]}
        onTransition={vi.fn()}
        newVersion={{ disabled: true, disabledReason: 'Available after approval', onSelect: onNewVersion }}
      />,
    );
    await openMenu();
    const item = await screen.findByRole('menuitem', { name: 'New version' });
    expect(item).toHaveAttribute('aria-disabled', 'true');
    expect(item.parentElement).toHaveAttribute('title', 'Available after approval');
  });
});
