import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, ChevronsUpDown, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { useDomains } from '@/hooks/use-domains';
import type { GateExemptions } from '@/types/process-workflow';

interface Option {
  id: string;
  name: string;
}

/** The ``{id, name}`` items of a list response (a plain array or ``{items: [...]}``). Pure. */
export function toOptions(data: unknown): Option[] {
  const rows = Array.isArray(data)
    ? data
    : Array.isArray((data as { items?: unknown[] } | null)?.items)
      ? (data as { items: unknown[] }).items
      : [];
  return rows
    .map((row) => row as { id?: unknown; name?: unknown })
    .filter((row) => typeof row.id === 'string')
    .map((row) => ({ id: row.id as string, name: typeof row.name === 'string' && row.name ? row.name : (row.id as string) }));
}

export const EMPTY_GATE_EXEMPTIONS: GateExemptions = { domains: [], data_products: [], data_contracts: [] };

function useEntityOptions(url: string): Option[] {
  const [options, setOptions] = useState<Option[]>([]);
  useEffect(() => {
    let cancelled = false;
    fetch(url)
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        if (!cancelled) setOptions(toOptions(data));
      })
      .catch(() => {
        if (!cancelled) setOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [url]);
  return options;
}

interface PickerProps {
  id: string;
  label: string;
  options: Option[];
  value: string[];
  onChange: (ids: string[]) => void;
}

function Picker({ id, label, options, value, onChange }: PickerProps) {
  const { t } = useTranslation('workflows');
  const [open, setOpen] = useState(false);
  const nameOf = (optionId: string) => options.find((o) => o.id === optionId)?.name ?? optionId;
  const toggle = (optionId: string) =>
    onChange(value.includes(optionId) ? value.filter((v) => v !== optionId) : [...value, optionId]);

  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1" data-testid={`${id}-selected`}>
          {value.map((optionId) => (
            <Badge key={optionId} variant="secondary" className="flex items-center gap-1">
              <span className="truncate max-w-[12rem]">{nameOf(optionId)}</span>
              <button
                type="button"
                onClick={() => toggle(optionId)}
                aria-label={t('designer.triggerConfig.disabledFor.remove', { name: nameOf(optionId) })}
                className="focus:outline-none"
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button id={id} variant="outline" role="combobox" aria-expanded={open} className="w-full justify-between">
            <span className="truncate text-muted-foreground">
              {value.length > 0
                ? t('designer.triggerConfig.disabledFor.selectedCount', { count: value.length })
                : t('designer.triggerConfig.disabledFor.none')}
            </span>
            <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-full p-0 pointer-events-auto" align="start">
          <Command>
            <CommandInput placeholder={t('designer.triggerConfig.disabledFor.search')} />
            <CommandList>
              <CommandEmpty>{t('designer.triggerConfig.disabledFor.noResults')}</CommandEmpty>
              <CommandGroup>
                {options.map((option) => (
                  <CommandItem key={option.id} value={`${option.name} ${option.id}`} onSelect={() => toggle(option.id)}>
                    <Check className={cn('mr-2 h-4 w-4', value.includes(option.id) ? 'opacity-100' : 'opacity-0')} />
                    {option.name}
                  </CommandItem>
                ))}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
    </div>
  );
}

interface GateExemptionsFieldProps {
  value: GateExemptions;
  onChange: (value: GateExemptions) => void;
}

/** Domains, data products and data contracts a before_status_change gate is switched off for. */
export default function GateExemptionsField({ value, onChange }: GateExemptionsFieldProps) {
  const { t } = useTranslation('workflows');
  const { domains } = useDomains();
  const products = useEntityOptions('/api/data-products');
  const contracts = useEntityOptions('/api/data-contracts');
  const domainOptions = domains.map((d) => ({ id: d.id, name: d.name || d.id }));

  return (
    <div className="space-y-2 rounded-md border p-3" data-testid="gate-exemptions">
      <div className="space-y-1">
        <Label>{t('designer.triggerConfig.disabledFor.title')}</Label>
        <p className="text-xs text-muted-foreground">{t('designer.triggerConfig.disabledFor.hint')}</p>
      </div>
      <Picker
        id="gate-disabled-domains"
        label={t('designer.triggerConfig.disabledFor.domains')}
        options={domainOptions}
        value={value.domains}
        onChange={(ids) => onChange({ ...value, domains: ids })}
      />
      <Picker
        id="gate-disabled-products"
        label={t('designer.triggerConfig.disabledFor.dataProducts')}
        options={products}
        value={value.data_products}
        onChange={(ids) => onChange({ ...value, data_products: ids })}
      />
      <Picker
        id="gate-disabled-contracts"
        label={t('designer.triggerConfig.disabledFor.dataContracts')}
        options={contracts}
        value={value.data_contracts}
        onChange={(ids) => onChange({ ...value, data_contracts: ids })}
      />
    </div>
  );
}
