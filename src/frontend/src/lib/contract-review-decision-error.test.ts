/**
 * The contract page's Approve / Reject errors in the user's language (#1078).
 */
import { describe, it, expect, afterEach } from 'vitest';
import i18n from '@/i18n/config';
import { contractReviewDecisionErrorText } from './contract-review-decision-error';

const INVALID = {
  error: 'Invalid transition from approved to APPROVED',
  errorCode: 'invalid_status_transition',
  errorParams: { fromStatus: 'approved', toStatus: 'approved' },
};

const BLOCKED = {
  error: "Status transition from 'proposed' to 'approved' blocked by workflow: "
    + 'Contract Gate: Add a quality section first',
  errorCode: 'status_change_blocked',
  errorParams: {
    fromStatus: 'proposed',
    toStatus: 'approved',
    reasons: [{ workflow: 'Contract Gate', text: 'Add a quality section first' }],
  },
};

describe('contractReviewDecisionErrorText', () => {
  afterEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('names the status the contract has now when it no longer allows the decision', async () => {
    expect(contractReviewDecisionErrorText(INVALID, i18n.t, 'Approve failed')).toBe(
      'The contract is now Approved and can no longer be approved or rejected. '
      + 'Reload the page to see its current status.',
    );

    await i18n.changeLanguage('pt');

    expect(contractReviewDecisionErrorText(INVALID, i18n.t, 'Falha ao aprovar')).toBe(
      'O contrato agora está com o status Aprovado e não pode mais ser aprovado nem rejeitado. '
      + 'Recarregue a página para ver o status atual.',
    );
  });

  it('translates a workflow gate that blocks the decision', async () => {
    await i18n.changeLanguage('pt');

    expect(contractReviewDecisionErrorText(BLOCKED, i18n.t, 'Falha ao aprovar')).toBe(
      'A mudança de status de Proposto para Aprovado foi bloqueada por um workflow: '
      + 'Contract Gate: Add a quality section first',
    );
  });

  it('shows any other error as the API sent it', () => {
    expect(contractReviewDecisionErrorText(
      { error: 'Contract not found', errorCode: null, errorParams: null }, i18n.t, 'Approve failed',
    )).toBe('Contract not found');
  });

  it('falls back to the generic text when the API sent none', () => {
    expect(contractReviewDecisionErrorText(
      { error: '', errorCode: null, errorParams: null }, i18n.t, 'Reject failed',
    )).toBe('Reject failed');
  });

  it('keeps the English message for a code without its statuses', () => {
    expect(contractReviewDecisionErrorText(
      { ...INVALID, errorParams: null }, i18n.t, 'Approve failed',
    )).toBe('Invalid transition from approved to APPROVED');
  });
});
