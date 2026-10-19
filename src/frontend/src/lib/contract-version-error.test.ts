/**
 * "Create New Version" on a contract: a version not in the X.Y.Z form is
 * explained in the user's language, from the error's code and version.
 */
import { describe, it, expect, afterEach } from 'vitest';
import i18n from '@/i18n/config';
import { contractVersionErrorText } from './contract-version-error';

const REFUSED = {
  errorCode: 'invalid_version_format',
  errorParams: { version: '2.1' },
};

describe('contractVersionErrorText', () => {
  afterEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('explains a version not in the X.Y.Z form', () => {
    expect(contractVersionErrorText(REFUSED, '2.1', i18n.t)).toBe(
      '"2.1" is not a valid version: a new version uses the X.Y.Z form, for example 2.0.0.',
    );
  });

  it('says it in Portuguese', async () => {
    await i18n.changeLanguage('pt');

    expect(contractVersionErrorText(REFUSED, '2.1', i18n.t)).toBe(
      '"2.1" não é uma versão válida: uma nova versão usa o formato X.Y.Z, por exemplo 2.0.0.',
    );
  });

  it('gives the generic message for any other failure', () => {
    expect(contractVersionErrorText({ errorCode: null, errorParams: null }, '2.0.0', i18n.t)).toBe(
      'Failed to create new version.',
    );
  });
});
