import type { TFunction } from 'i18next';
import type { ApiResponse } from '@/hooks/use-api';
import { statusChangeErrorText } from '@/lib/status-change-error';

/**
 * The error of an Approve or Reject on the contract page, in the user's language.
 *
 * A workflow gate that blocks the change answers `status_change_blocked`, and
 * a contract whose status no longer allows the decision (approved or rejected
 * elsewhere meanwhile) answers `invalid_status_transition` with its status.
 * Any other error is shown as the API sent it, and `fallback` when it sent
 * none.
 */
export function contractReviewDecisionErrorText(
  response: Pick<ApiResponse<unknown>, 'error' | 'errorCode' | 'errorParams'>,
  t: TFunction,
  fallback: string,
): string {
  if (response.errorCode === 'invalid_status_transition' && response.errorParams) {
    const status = String(response.errorParams.fromStatus ?? '');
    return t('data-contracts:detailsView.errors.invalidTransition', {
      fromStatus: t(`data-contracts:status.${status}`, { defaultValue: status }),
      defaultValue: response.error || fallback,
    });
  }
  return statusChangeErrorText(response, t, 'data-contracts') || fallback;
}
