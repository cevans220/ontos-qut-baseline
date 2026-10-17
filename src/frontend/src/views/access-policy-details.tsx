import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Copy,
  Download,
  GitBranch,
  Pencil,
  Trash2,
} from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import DomainBadgeList from '@/components/ui/domain-badge-list';
import TagChip from '@/components/ui/tag-chip';
import { DetailViewSkeleton } from '@/components/common/list-view-skeleton';
import { CommentSidebar } from '@/components/comments';
import EntityMetadataPanel from '@/components/metadata/entity-metadata-panel';
import { useApi } from '@/hooks/use-api';
import { useToast } from '@/hooks/use-toast';
import { useTeams } from '@/hooks/use-teams';
import { useAccessCollections, useOdrlProfile } from '@/hooks/use-access-policies';
import useBreadcrumbStore from '@/stores/breadcrumb-store';
import { usePermissions } from '@/stores/permissions-store';
import { useProjectContext } from '@/stores/project-store';
import { FeatureAccessLevel } from '@/types/settings';
import AccessEntityFormDialog from '@/components/access-policies/access-entity-form-dialog';
import AccessStatusBadge from '@/components/access-policies/access-status-badge';
import CheckAccessPanel from '@/components/access-policies/check-access-panel';
import { ENFORCEMENT_VARIANT } from '@/components/access-policies/constraint-editor';
import { RULE_KINDS, describeConstraint, describeRef } from '@/components/access-policies/odrl-helpers';
import type {
  AccessCollection,
  AccessEntity,
  AccessKind,
  AccessPolicy,
  AccessStatus,
  OdrlConstraint,
  OdrlRule,
  PolicyValidation,
  VersionRow,
} from '@/types/access-policy';
import { KIND_ENTITY_TYPE, KIND_SEGMENT, SEGMENT_KIND } from '@/types/access-policy';

const FEATURE_ID = 'access-policies';

function bumpMinor(v: string): string {
  const m = /^(\d+)\.(\d+)\.(\d+)$/.exec(v || '');
  return m ? `${m[1]}.${Number(m[2]) + 1}.0` : `${v}-next`;
}

export default function AccessPolicyDetails() {
  const { t } = useTranslation(['access-policies', 'common']);
  const { segment = 'policies', entityId } = useParams<{ segment: string; entityId: string }>();
  const kind: AccessKind = SEGMENT_KIND[segment] || 'policy';
  const navigate = useNavigate();
  const { get, post, delete: del } = useApi();
  const { toast } = useToast();
  const { hasPermission } = usePermissions();
  const canWrite = hasPermission(FEATURE_ID, FeatureAccessLevel.READ_WRITE);
  const { teams } = useTeams();
  const { availableProjects, fetchUserProjects } = useProjectContext();
  const profile = useOdrlProfile();
  const { nameForIri } = useAccessCollections();
  const setStaticSegments = useBreadcrumbStore((s) => s.setStaticSegments);
  const setDynamicTitle = useBreadcrumbStore((s) => s.setDynamicTitle);

  const [entity, setEntity] = useState<AccessEntity | null>(null);
  const [versions, setVersions] = useState<VersionRow[]>([]);
  const [validation, setValidation] = useState<PolicyValidation | null>(null);
  const [jsonld, setJsonld] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [commentsOpen, setCommentsOpen] = useState(false);
  const [versionOpen, setVersionOpen] = useState(false);
  const [newVersion, setNewVersion] = useState('');
  const [changeSummary, setChangeSummary] = useState('');
  const [statusTarget, setStatusTarget] = useState<AccessStatus | null>(null);
  const [statusComment, setStatusComment] = useState('');
  const [deleteOpen, setDeleteOpen] = useState(false);

  const base = `/api/access-policies/${KIND_SEGMENT[kind]}/${entityId}`;

  const load = useCallback(async () => {
    if (!entityId) return;
    setLoading(true);
    setError(null);
    let res = await get<AccessEntity>(base);
    if (!res.data || res.error) {
      // Read-after-write: the create response can precede the commit.
      await new Promise((r) => setTimeout(r, 400));
      res = await get<AccessEntity>(base);
    }
    if (!res.data || res.error) {
      setError(res.error || t('access-policies:details.notFound'));
      setEntity(null);
      setLoading(false);
      return;
    }
    setEntity(res.data);
    setDynamicTitle(res.data.name);
    const v = await get<VersionRow[]>(`${base}/versions`);
    setVersions(Array.isArray(v.data) ? v.data : []);
    if (kind === 'policy') {
      const [val, doc] = await Promise.all([get<PolicyValidation>(`${base}/validation`), get<unknown>(`${base}/jsonld`)]);
      setValidation(val.data || null);
      setJsonld(doc.data || null);
    }
    setLoading(false);
  }, [entityId, base, get, kind, setDynamicTitle, t]);

  useEffect(() => {
    fetchUserProjects();
    setStaticSegments([
      { label: t('access-policies:title'), path: '/access-policies' },
      { label: t(`access-policies:tabs.${kind === 'asset_collection' ? 'assetCollections' : kind === 'audience' ? 'audiences' : 'policies'}`), path: `/access-policies?tab=${KIND_SEGMENT[kind]}` },
    ]);
    load();
    return () => {
      setStaticSegments([]);
      setDynamicTitle(null);
    };
  }, [load, kind, setStaticSegments, setDynamicTitle, fetchUserProjects, t]);

  const actionLabel = useCallback(
    (k: string | null | undefined) => profile?.actions.find((a) => a.key === k)?.label || k || '?',
    [profile],
  );
  const operandLabel = useCallback(
    (k: string) => profile?.left_operands.find((o) => o.key === k)?.label || k,
    [profile],
  );
  const enforcementOf = useCallback(
    (c: OdrlConstraint): string[] =>
      c.logical
        ? (c.constraints || []).flatMap(enforcementOf)
        : [profile?.left_operands.find((o) => o.key === c.leftOperand)?.enforcement || 'platform'],
    [profile],
  );

  const changeStatus = async () => {
    if (!statusTarget) return;
    const res = await post<AccessEntity>(`${base}/status`, { status: statusTarget, comment: statusComment || undefined });
    if (res.error || !res.data) {
      toast({ title: t('access-policies:details.statusFailed'), description: res.error || '', variant: 'destructive' });
    } else {
      toast({ title: t('access-policies:details.statusChanged', { status: t(`access-policies:status.${statusTarget}`) }) });
      setEntity(res.data);
      load();
    }
    setStatusTarget(null);
    setStatusComment('');
  };

  const createVersion = async () => {
    const res = await post<AccessEntity>(`${base}/versions`, { new_version: newVersion, change_summary: changeSummary || undefined });
    if (res.error || !res.data) {
      toast({ title: t('access-policies:details.versionFailed'), description: res.error || '', variant: 'destructive' });
      return;
    }
    setVersionOpen(false);
    toast({ title: t('access-policies:details.versionCreated', { version: res.data.version }) });
    navigate(`/access-policies/${KIND_SEGMENT[kind]}/${res.data.id}`);
  };

  const doDelete = async () => {
    const res = await del(base);
    setDeleteOpen(false);
    if (res.error) {
      toast({ title: t('access-policies:details.deleteFailed'), description: res.error, variant: 'destructive' });
      return;
    }
    toast({ title: t('access-policies:details.deleted') });
    navigate(`/access-policies?tab=${KIND_SEGMENT[kind]}`);
  };

  const downloadJsonld = () => {
    if (!jsonld || !entity) return;
    const blob = new Blob([JSON.stringify(jsonld, null, 2)], { type: 'application/ld+json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${entity.name.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}-${entity.version}.jsonld`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const teamName = useMemo(() => teams.find((x) => x.id === entity?.owner_team_id)?.name, [teams, entity]);
  const projectName = useMemo(
    () => availableProjects.find((x) => x.id === entity?.project_id)?.name,
    [availableProjects, entity],
  );

  if (loading && !entity) return <DetailViewSkeleton />;
  if (error || !entity) {
    return (
      <div className="space-y-4 py-6">
        <Button variant="outline" size="sm" onClick={() => navigate('/access-policies')}>
          <ArrowLeft className="mr-2 h-4 w-4" /> {t('access-policies:details.back')}
        </Button>
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{error || t('access-policies:details.notFound')}</AlertDescription>
        </Alert>
      </div>
    );
  }

  const policy = kind === 'policy' ? (entity as AccessPolicy) : null;
  const collection = kind !== 'policy' ? (entity as AccessCollection) : null;
  const canDelete = canWrite && (entity.status === 'draft' || entity.status === 'retired');

  const renderRule = (r: OdrlRule, kindName: string, idx: number) => {
    const enforcement = Array.from(new Set((r.constraint || []).flatMap(enforcementOf)));
    const subs = kindName === 'prohibition' ? r.remedy : r.duty;
    return (
      <div key={`${kindName}-${idx}`} className="rounded-md border p-3 text-sm" data-testid={`rule-view-${kindName}-${idx}`}>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={kindName === 'prohibition' ? 'destructive' : kindName === 'obligation' ? 'outline' : 'secondary'}>
            {t(`access-policies:ruleKinds.${kindName}`)}
          </Badge>
          <span>
            <strong>{r.assignee ? describeRef(r.assignee, nameForIri) : t('access-policies:details.everyone')}</strong>{' '}
            {t(`access-policies:details.verb.${kindName}`)} <strong>{actionLabel(r.action)}</strong>
            {r.target ? (
              <>
                {' '}
                {t('access-policies:details.on')} <code className="rounded bg-muted px-1">{describeRef(r.target, nameForIri)}</code>
              </>
            ) : null}
          </span>
        </div>
        {(r.constraint || []).length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>{t('access-policies:details.when')}</span>
            {(r.constraint || []).map((c, i) => (
              <code key={i} className="rounded bg-muted px-1">
                {describeConstraint(c, operandLabel)}
              </code>
            ))}
            {enforcement.map((e) => (
              <Badge key={e} variant={ENFORCEMENT_VARIANT[e]} className="text-[10px]">
                {t(`access-policies:enforcement.${e}`)}
              </Badge>
            ))}
          </div>
        )}
        {subs && subs.length > 0 && (
          <div className="mt-2 text-xs">
            <span className="text-muted-foreground">
              {kindName === 'prohibition' ? t('access-policies:details.remedy') : t('access-policies:details.duty')}:
            </span>{' '}
            {subs.map((d) => actionLabel(d.action)).join(', ')}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-6 py-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button variant="outline" size="sm" onClick={() => navigate(`/access-policies?tab=${KIND_SEGMENT[kind]}`)}>
          <ArrowLeft className="mr-2 h-4 w-4" /> {t('access-policies:details.back')}
        </Button>
        <div className="flex flex-wrap items-center gap-2">
          {canWrite &&
            entity.allowed_transitions.map((s) => (
              <Button
                key={s}
                size="sm"
                variant={s === 'active' || s === 'approved' ? 'default' : 'outline'}
                onClick={() => setStatusTarget(s)}
                data-testid={`transition-${s}`}
              >
                {t(`access-policies:transitions.${s}`)}
              </Button>
            ))}
          {canWrite && (
            <Button size="sm" variant="outline" onClick={() => setEditOpen(true)}>
              <Pencil className="mr-1 h-4 w-4" /> {t('common:actions.edit')}
            </Button>
          )}
          {canWrite && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setNewVersion(bumpMinor(entity.version));
                setChangeSummary('');
                setVersionOpen(true);
              }}
            >
              <GitBranch className="mr-1 h-4 w-4" /> {t('access-policies:details.newVersion')}
            </Button>
          )}
          <CommentSidebar
            entityType={KIND_ENTITY_TYPE[kind]}
            entityId={entity.id}
            isOpen={commentsOpen}
            onToggle={() => setCommentsOpen(!commentsOpen)}
            className="h-8"
          />
          {canDelete && (
            <Button size="sm" variant="destructive" onClick={() => setDeleteOpen(true)}>
              <Trash2 className="mr-1 h-4 w-4" /> {t('common:actions.delete')}
            </Button>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-3xl font-bold">{entity.name}</h1>
        <AccessStatusBadge status={entity.status} />
        {versions.length > 1 ? (
          <Select value={entity.id} onValueChange={(id) => navigate(`/access-policies/${KIND_SEGMENT[kind]}/${id}`)}>
            <SelectTrigger className="h-8 w-44" aria-label={t('common:labels.version')}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {versions.map((v) => (
                <SelectItem key={v.id} value={v.id}>
                  v{v.version} · {t(`access-policies:status.${v.status}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <Badge variant="outline">v{entity.version}</Badge>
        )}
        {policy && <Badge variant="outline">{t(`access-policies:policyTypes.${policy.policy_type}`)}</Badge>}
        {entity.draft_owner_id && <Badge variant="outline">{t('access-policies:list.personalDraft')}</Badge>}
      </div>
      {entity.description && <p className="max-w-4xl text-muted-foreground">{entity.description}</p>}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-1">
          <CardHeader>
            <CardTitle className="text-base">{t('access-policies:details.overview')}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div>
              <div className="text-xs text-muted-foreground">{t('access-policies:details.iri')}</div>
              <div className="flex items-center gap-1">
                <code className="break-all text-xs">{entity.uid}</code>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6"
                  aria-label={t('access-policies:details.copyIri')}
                  onClick={() => navigator.clipboard?.writeText(entity.uid)}
                >
                  <Copy className="h-3 w-3" />
                </Button>
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">{t('access-policies:form.ownerTeam')}</div>
              <div>{teamName || t('access-policies:form.none')}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">{t('access-policies:form.project')}</div>
              <div>{projectName || (entity.project_id ? entity.project_id : t('access-policies:form.none'))}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">{t('access-policies:form.domains')}</div>
              {entity.domains.length ? <DomainBadgeList domains={entity.domains as any} /> : <div>{t('access-policies:form.none')}</div>}
            </div>
            <div>
              <div className="text-xs text-muted-foreground">{t('access-policies:form.tags')}</div>
              <div className="flex flex-wrap gap-1">
                {entity.tags.length ? entity.tags.map((tag, i) => <TagChip key={i} tag={tag} size="sm" />) : t('access-policies:form.none')}
              </div>
            </div>
            {policy && (
              <>
                <div>
                  <div className="text-xs text-muted-foreground">{t('access-policies:form.conflict')}</div>
                  <div>{t(`access-policies:conflicts.${policy.conflict}`)}</div>
                </div>
                {policy.inherit_from.length > 0 && (
                  <div>
                    <div className="text-xs text-muted-foreground">{t('access-policies:form.inheritFrom')}</div>
                    {policy.inherit_from.map((p) => (
                      <code key={p} className="block break-all text-xs">
                        {p}
                      </code>
                    ))}
                  </div>
                )}
              </>
            )}
            {entity.change_summary && (
              <div>
                <div className="text-xs text-muted-foreground">{t('access-policies:details.changeSummary')}</div>
                <div>{entity.change_summary}</div>
              </div>
            )}
            <div className="text-xs text-muted-foreground">
              {t('access-policies:details.createdBy', { user: entity.created_by || '?' })}
              {entity.updated_at ? ` · ${new Date(entity.updated_at).toLocaleString()}` : ''}
            </div>
          </CardContent>
        </Card>

        <div className="space-y-6 lg:col-span-2">
          {policy && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">{t('access-policies:details.rules')}</CardTitle>
                <CardDescription>{t('access-policies:details.rulesHint')}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-2">
                {RULE_KINDS.flatMap((k) => (policy.rules[k] || []).map((r, i) => renderRule(r, k, i)))}
                {RULE_KINDS.every((k) => !(policy.rules[k] || []).length) && (
                  <p className="text-sm text-muted-foreground">{t('access-policies:rules.empty')}</p>
                )}
              </CardContent>
            </Card>
          )}

          {policy && validation && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">{t('access-policies:details.validation')}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1 text-sm" data-testid="policy-validation">
                {validation.issues.length === 0 && (validation.activation_problems || []).length === 0 ? (
                  <p className="flex items-center gap-2 text-emerald-700 dark:text-emerald-400">
                    <CheckCircle2 className="h-4 w-4" /> {t('access-policies:details.readyToActivate')}
                  </p>
                ) : (
                  <>
                    {validation.issues.map((i, idx) => (
                      <p key={idx} className={i.severity === 'error' ? 'text-destructive' : 'text-amber-700 dark:text-amber-400'}>
                        <AlertTriangle className="mr-1 inline h-3 w-3" />
                        <code className="text-xs">{i.path || 'policy'}</code> — {i.message}
                      </p>
                    ))}
                    {(validation.activation_problems || [])
                      .filter((p) => !validation.issues.some((i) => i.message === p))
                      .map((p, idx) => (
                        <p key={`a${idx}`} className="text-amber-700 dark:text-amber-400">
                          <AlertTriangle className="mr-1 inline h-3 w-3" />
                          {p}
                        </p>
                      ))}
                  </>
                )}
              </CardContent>
            </Card>
          )}

          {collection && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">
                  {kind === 'audience' ? t('access-policies:details.audienceDefinition') : t('access-policies:details.collectionDefinition')}
                </CardTitle>
                <CardDescription>
                  {kind === 'audience' ? t('access-policies:details.audienceSemantics') : t('access-policies:details.collectionSemantics')}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <div>
                  <div className="text-xs text-muted-foreground">
                    {kind === 'audience' ? t('access-policies:form.principals') : t('access-policies:form.assets')}
                  </div>
                  {collection.members.length ? (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {collection.members.map((m) => (
                        <Badge key={m} variant="secondary" className="font-mono text-xs">
                          {m}
                        </Badge>
                      ))}
                    </div>
                  ) : (
                    <div className="text-muted-foreground">
                      {kind === 'audience' ? t('access-policies:details.allPrincipals') : t('access-policies:details.allAssets')}
                    </div>
                  )}
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">{t('access-policies:form.refinement')}</div>
                  {collection.refinement.length ? (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {collection.refinement.map((c, i) => (
                        <code key={i} className="rounded bg-muted px-1 text-xs">
                          {describeConstraint(c, operandLabel)}
                        </code>
                      ))}
                    </div>
                  ) : (
                    <div className="text-muted-foreground">{t('access-policies:form.none')}</div>
                  )}
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">{t('access-policies:details.usedBy')}</div>
                  {collection.used_by.length ? (
                    <ul className="mt-1 space-y-1">
                      {collection.used_by.map((p) => (
                        <li key={p.id} className="flex items-center gap-2">
                          <Link to={`/access-policies/policies/${p.id}`} className="text-primary hover:underline">
                            {p.name}
                          </Link>
                          <span className="text-xs text-muted-foreground">v{p.version}</span>
                          <AccessStatusBadge status={p.status} />
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div className="text-muted-foreground">{t('access-policies:details.notUsed')}</div>
                  )}
                </div>
              </CardContent>
            </Card>
          )}

          {policy && <CheckAccessPanel policyId={policy.id} />}

          {policy && jsonld != null && (
            <Card>
              <CardHeader className="flex flex-row items-center justify-between space-y-0">
                <div>
                  <CardTitle className="text-base">{t('access-policies:details.jsonld')}</CardTitle>
                  <CardDescription>{t('access-policies:details.jsonldHint')}</CardDescription>
                </div>
                <Button size="sm" variant="outline" onClick={downloadJsonld}>
                  <Download className="mr-1 h-4 w-4" /> {t('access-policies:details.download')}
                </Button>
              </CardHeader>
              <CardContent>
                <pre className="max-h-96 overflow-auto rounded bg-muted p-3 text-xs" data-testid="policy-jsonld">
                  {JSON.stringify(jsonld, null, 2)}
                </pre>
              </CardContent>
            </Card>
          )}
        </div>
      </div>

      <EntityMetadataPanel entityId={entity.id} entityType={KIND_ENTITY_TYPE[kind]} />

      <AccessEntityFormDialog
        kind={kind}
        open={editOpen}
        onOpenChange={setEditOpen}
        entity={entity}
        onSaved={() => load()}
      />

      <Dialog open={!!statusTarget} onOpenChange={(o) => !o && setStatusTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {statusTarget ? t(`access-policies:transitions.${statusTarget}`) : ''}
            </DialogTitle>
            <DialogDescription>
              {statusTarget
                ? t('access-policies:details.statusConfirm', {
                    from: t(`access-policies:status.${entity.status}`),
                    to: t(`access-policies:status.${statusTarget}`),
                  })
                : ''}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1">
            <Label htmlFor="ap-status-comment">{t('access-policies:details.statusComment')}</Label>
            <Textarea id="ap-status-comment" rows={3} value={statusComment} onChange={(e) => setStatusComment(e.target.value)} />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setStatusTarget(null)}>
              {t('common:actions.cancel')}
            </Button>
            <Button onClick={changeStatus} data-testid="confirm-transition">
              {t('access-policies:details.confirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={versionOpen} onOpenChange={setVersionOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('access-policies:details.newVersion')}</DialogTitle>
            <DialogDescription>{t('access-policies:details.newVersionHint', { version: entity.version })}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="ap-new-version">{t('common:labels.version')}</Label>
              <Input id="ap-new-version" value={newVersion} onChange={(e) => setNewVersion(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="ap-change-summary">{t('access-policies:details.changeSummary')}</Label>
              <Textarea id="ap-change-summary" rows={3} value={changeSummary} onChange={(e) => setChangeSummary(e.target.value)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setVersionOpen(false)}>
              {t('common:actions.cancel')}
            </Button>
            <Button onClick={createVersion} disabled={!newVersion.trim()}>
              {t('common:actions.create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('access-policies:details.deleteTitle')}</DialogTitle>
            <DialogDescription>{t('access-policies:details.deleteConfirm', { name: entity.name, version: entity.version })}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              {t('common:actions.cancel')}
            </Button>
            <Button variant="destructive" onClick={doDelete}>
              {t('common:actions.delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
