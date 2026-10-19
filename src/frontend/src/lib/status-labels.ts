import type { TFunction } from 'i18next';

// Status values the API sends as they are, shown in the user's language.
// An unknown value is shown as sent.

/** A workflow execution's status (pending, running, paused, succeeded, failed, cancelled). */
export function executionStatusLabel(status: string, t: TFunction): string {
  return t(`workflows:execution.executionStates.${status}`, { defaultValue: status });
}

/** The status of an asset review request or of an asset in it. */
export function reviewStatusLabel(status: string, t: TFunction): string {
  return t(`data-asset-reviews:status.${status}`, { defaultValue: status });
}
