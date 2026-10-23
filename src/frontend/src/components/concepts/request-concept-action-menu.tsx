import { useTranslation } from 'react-i18next';
import { ChevronDown, CopyPlus, Send } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

// ---------------------------------------------------------------------------
// RequestConceptActionMenu — the concept detail page's single "Request" menu
// (mirrors the data-product "Request" entry point). It consolidates:
//   * the lifecycle step(s) valid from the current status (Request review for a
//     draft; approve / request changes / publish / certify / deprecate /
//     archive afterwards), and
//   * "New version" (opens the PublishVersionDialog in the parent).
//
// The menu is presentational: the parent decides WHICH transitions the user
// may run (today's permission logic) and owns every side effect. It renders
// nothing when there is nothing to offer.
// ---------------------------------------------------------------------------

export interface ConceptTransitionItem {
  /** Target status, e.g. 'under_review'. */
  status: string;
  /** by-iri action verb, e.g. 'submit-review', 'approve'. */
  action: string;
  labelKey: string;
  defaultLabel: string;
}

export interface ConceptNewVersionOption {
  disabled: boolean;
  /** Why it is disabled; shown as the item's tooltip. */
  disabledReason?: string;
  onSelect: () => void;
}

export interface RequestConceptActionMenuProps {
  transitions: ConceptTransitionItem[];
  onTransition: (status: string, action: string) => void;
  /** Omit to hide "New version" entirely (no permission / not versionable). */
  newVersion?: ConceptNewVersionOption | null;
  busy?: boolean;
}

export function RequestConceptActionMenu({
  transitions,
  onTransition,
  newVersion,
  busy = false,
}: RequestConceptActionMenuProps) {
  const { t } = useTranslation(['semantic-models', 'common']);

  if (transitions.length === 0 && !newVersion) return null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" disabled={busy}>
          <Send className="mr-2 h-4 w-4" />
          {t('semantic-models:lifecycle.request', 'Request')}
          <ChevronDown className="ml-2 h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {transitions.map((item) => (
          <DropdownMenuItem
            key={item.action || item.status}
            onSelect={() => onTransition(item.status, item.action)}
            className="font-medium"
          >
            {item.action === 'submit-review'
              ? t('semantic-models:lifecycle.requestReview', 'Request review')
              : t(item.labelKey, item.defaultLabel)}
          </DropdownMenuItem>
        ))}
        {newVersion && transitions.length > 0 && <DropdownMenuSeparator />}
        {newVersion && (
          // Wrapping span carries the title so the reason still shows while the
          // item is disabled (disabled items don't fire hover events).
          <span title={newVersion.disabled ? newVersion.disabledReason : undefined}>
            <DropdownMenuItem
              disabled={newVersion.disabled}
              onSelect={() => newVersion.onSelect()}
            >
              <CopyPlus className="mr-2 h-4 w-4" />
              {t('semantic-models:versionHistory.newVersion', 'New version')}
            </DropdownMenuItem>
          </span>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export default RequestConceptActionMenu;
