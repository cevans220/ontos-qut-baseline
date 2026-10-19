import { describe, it, expect } from 'vitest';
import type { SemanticModel } from '@/types/ontology';
import { isSystemSemanticModel } from './semantic-models-settings';

// Built-in ontologies (created_by `system@...`) are hidden by default in the
// RDF sources list; this predicate decides which rows count as built-in.
const model = (created_by?: string) => ({ id: 'x', name: 'x', enabled: true, created_by }) as SemanticModel;

describe('isSystemSemanticModel', () => {
  it('treats system@ creators as system ontologies', () => {
    expect(isSystemSemanticModel(model('system@startup'))).toBe(true);
    expect(isSystemSemanticModel(model('system@file'))).toBe(true);
  });

  it('treats user uploads and missing creators as non-system', () => {
    expect(isSystemSemanticModel(model('someone@example.com'))).toBe(false);
    expect(isSystemSemanticModel(model(undefined))).toBe(false);
  });
});
