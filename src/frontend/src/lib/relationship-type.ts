import type { RelationshipDefinition } from '@/types/ontology-schema';

/**
 * Machine type name (IRI local name, e.g. "DataProduct") of a relationship's
 * target type. Use this wherever a type name is sent to the API or used as a
 * lookup key; `target_type_label` is the human-readable rdfs:label
 * ("Data Product") and is for display only.
 */
export function getTargetTypeName(
  rel: Pick<RelationshipDefinition, 'target_type_iri' | 'target_type_label'>,
): string {
  const iri = rel.target_type_iri ?? '';
  const localName = iri.split(/[#/]/).pop();
  if (localName) return localName;
  return (rel.target_type_label ?? '').replace(/\s+/g, '');
}
