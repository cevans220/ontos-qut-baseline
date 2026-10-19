import type { TFunction } from 'i18next';
import type { ApiResponse } from '@/hooks/use-api';

/**
 * The message for a failed "Create New Version" (`POST /clone`): a version not in the
 * X.Y.Z form comes back with the code `invalid_version_format` and is shown in the
 * user's language; any other failure gets the generic message.
 */
export function contractVersionErrorText(
  response: Pick<ApiResponse<unknown>, 'errorCode' | 'errorParams'>,
  version: string,
  t: TFunction,
): string {
  if (response.errorCode === 'invalid_version_format') {
    return t('data-contracts:detailsView.errors.invalidVersionFormat', {
      version: String(response.errorParams?.version ?? version),
    });
  }
  return t('data-contracts:detailsView.errors.createVersionFailed', 'Failed to create new version.');
}
