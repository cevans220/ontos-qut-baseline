/**
 * Every locale has the same keys as English in the data contract, data
 * product and workflow namespaces, so no text falls back to English.
 */
import { describe, expect, it } from 'vitest';

type Messages = Record<string, unknown>;

const FILES = import.meta.glob(
  './locales/*/{data-contracts,data-products,workflows}.json',
  { eager: true },
) as Record<string, { default: Messages }>;

function keys(messages: Messages, prefix = ''): string[] {
  return Object.entries(messages).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return value && typeof value === 'object' ? keys(value as Messages, path) : [path];
  });
}

const byNamespace: Record<string, Record<string, Messages>> = {};
for (const [path, module] of Object.entries(FILES)) {
  const [, locale, file] = /\.\/locales\/([^/]+)\/([^/]+)\.json$/.exec(path)!;
  (byNamespace[file] ??= {})[locale] = module.default;
}

describe.each(Object.keys(byNamespace))('%s', (namespace) => {
  const english = keys(byNamespace[namespace].en).sort();

  it.each(Object.keys(byNamespace[namespace]).filter((l) => l !== 'en'))(
    '%s has the same keys as English',
    (locale) => {
      expect(keys(byNamespace[namespace][locale]).sort()).toEqual(english);
    },
  );
});
