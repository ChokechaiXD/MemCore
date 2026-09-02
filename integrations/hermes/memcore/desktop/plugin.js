// MemCore desktop UI — /memcore page, sidebar nav, palette command.
// Shape follows the unified plugin contract (ROUTES_AREA + SIDEBAR_NAV_AREA
// + PALETTE_AREA), data via ctx.rest -> dashboard/plugin_api.py.
// Theme vars only — no hardcoded colors. jsx() calls, no JSX syntax.
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Separator,
  Skeleton,
  useMutation,
  useQuery,
  useQueryClient,
  host
} from '@hermes/plugin-sdk'
import { useMemo, useState } from 'react'
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'memcore'
const PLUGIN_NAME = 'MemCore — Shared Project Memory'
const PLUGIN_ROUTE = '/memcore'
const MUTED = { color: 'var(--ui-text-secondary)' }
const FAINT = { color: 'var(--ui-text-quaternary)' }
const BORDER = { borderColor: 'var(--ui-stroke-secondary)' }

// Age shown on rows/versions/audit — coarse buckets, no per-render ticking.
function ageOf(value) {
  if (value === undefined || value === null || value === '') return null
  const numeric = Number(value)
  const ms = typeof value === 'number' || (typeof value === 'string' && value.trim() && Number.isFinite(numeric))
    ? (numeric < 1_000_000_000_000 ? numeric * 1000 : numeric)
    : Date.parse(value)
  if (!Number.isFinite(ms)) return null
  const seconds = Math.floor((Date.now() - ms) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

const VIEWS = [
  { key: 'candidate', label: 'Inbox' },
  { key: 'accepted', label: 'Shared' },
  { key: 'stale', label: 'Stale' },
  { key: 'conflict', label: 'Conflicts' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'tombstones', label: 'Tombstones' }
]

// Per-view empty copy: what this tab is and what to do next. Rejected and
// tombstones are healthy when empty — say so instead of implying a problem.
const EMPTY_COPY = {
  candidate: ['Inbox is empty', 'New memories proposed by agents land here for review.'],
  accepted: ['No shared memories yet', 'Promote a candidate from the Inbox to share it across agents.'],
  stale: ['Nothing is stale', 'Memories that go out of date appear here for review.'],
  conflict: ['No conflicts', 'Contradicting memories from different agents appear here.'],
  rejected: ['No rejected memories', 'Memories you reject are kept here for reference.'],
  tombstones: ['No tombstones', 'Deleted memories leave a tombstone here so they are not re-learned.']
}

function makePath(path, params = {}) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  })
  const query = search.toString()
  return query ? `${path}?${query}` : path
}

function createMemcoreApi(ctx) {
  const get = (path, params) => ctx.rest(makePath(path, params))
  const post = (path, body) => ctx.rest(path, { method: 'POST', body })
  return {
    state: () => get('/state'),
    memories: (view, project) => get('/memories', { state: view, project }),
    search: (q, project) => get('/search', { q, project }),
    detail: id => get(`/memory/${encodeURIComponent(id)}`),
    projects: () => get('/projects'),
    promote: memory_id => post('/promote', { memory_id }),
    pin: (memory_id, pinned) => post('/pin', { memory_id, pinned }),
    disable: memory_id => post('/disable', { memory_id })
  }
}

function Card({ className = '', children }) {
  return jsx('section', {
    className: `rounded-lg border ${className}`,
    style: BORDER,
    children
  })
}

function ScopeBadge({ item }) {
  if (!item.scope) return null
  const variant = item.pinned ? 'default' : 'outline'
  const label = item.pinned ? `${item.scope} · pinned` : item.scope
  return jsx(Badge, { variant, children: label })
}

function LifecycleBadge({ lifecycle, freshness }) {
  if (!lifecycle) return null
  const tone = lifecycle === 'accepted' ? 'default'
    : lifecycle === 'conflict' ? 'destructive'
    : lifecycle === 'rejected' ? 'outline' : 'secondary'
  const label = freshness === 'stale' ? `${lifecycle} · stale` : lifecycle
  return jsx(Badge, { variant: tone, children: label })
}

function Age({ value }) {
  const label = ageOf(value)
  if (!label) return null
  return jsx('span', {
    className: 'text-xs whitespace-nowrap',
    style: FAINT,
    title: String(value ?? ''),
    children: label
  })
}

function ErrorBox({ title, error, onRetry }) {
  const message = error instanceof Error ? error.message : String(error || '')
  return jsx('div', {
    className: 'flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-sm',
    style: { borderColor: 'var(--ui-red)', color: 'var(--ui-red)' },
    children: [
      jsx('span', { className: 'min-w-0', children: `${title}${message ? ` — ${message}` : ''}` }),
      onRetry ? jsx(Button, { variant: 'outline', size: 'sm', onClick: onRetry, children: 'Retry' }) : null
    ]
  })
}

function MemoryRow({ item, onOpen }) {
  return jsxs('button', {
    onClick: () => onOpen(item.id),
    className: 'w-full text-left px-3 py-2 hover:bg-[var(--ui-row-hover-background)] transition-colors',
    style: { background: 'transparent', cursor: 'pointer' },
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(ScopeBadge, { item }),
          jsx(LifecycleBadge, { lifecycle: item.lifecycle, freshness: item.freshness }),
          item.owner ? jsx('span', { className: 'text-xs', style: FAINT, children: item.owner }) : null,
          item.type ? jsx('span', { className: 'text-xs', style: FAINT, children: item.type }) : null,
          item.reason ? jsx('span', { className: 'text-xs', style: FAINT, children: `reason: ${item.reason}` }) : null,
          jsx('span', { className: 'flex-1' }),
          jsx(Age, { value: item.created_at })
        ]
      }),
      jsx('p', { className: 'mt-1 text-sm', children: item.content || item.fingerprint || item.id })
    ]
  }, item.id)
}

function MemoryList({ view, api, onOpen }) {
  const isSearch = view === '__search__'
  const listQuery = useQuery({
    queryKey: [PLUGIN_ID, 'list', view],
    queryFn: () => api.memories(view),
    enabled: !isSearch
  })
  if (isSearch) return null
  if (listQuery.isLoading) return jsx(Skeleton, { className: 'h-24 w-full' })
  if (listQuery.error) {
    return jsx(ErrorBox, {
      title: 'Failed to load memories',
      error: listQuery.error,
      onRetry: () => listQuery.refetch()
    })
  }
  const items = listQuery.data?.items || []
  if (!items.length) {
    const [title, description] = EMPTY_COPY[view] || ['Nothing here yet', null]
    return jsx(EmptyState, { title, description })
  }
  return jsx('div', { className: 'divide-y', style: BORDER, children: items.map(item => jsx(MemoryRow, { item, onOpen }, item.id)) })
}

function SearchResults({ query, api, onOpen }) {
  const searchQuery = useQuery({
    queryKey: [PLUGIN_ID, 'search', query],
    queryFn: () => api.search(query),
    enabled: query.trim().length > 0
  })
  if (!query.trim()) return null
  if (searchQuery.isLoading) return jsx(Skeleton, { className: 'h-16 w-full' })
  if (searchQuery.error) {
    return jsx(ErrorBox, {
      title: 'Search failed',
      error: searchQuery.error,
      onRetry: () => searchQuery.refetch()
    })
  }
  const items = searchQuery.data?.items || []
  if (!items.length) return jsx(EmptyState, { title: `No results for "${query}"`, description: 'Try fewer or different words.' })
  return jsx('div', { className: 'divide-y', style: BORDER, children: items.map(item => jsx(MemoryRow, { item, onOpen }, item.id)) })
}

function DetailActions({ item, api, queryClient }) {
  const invalidate = () => queryClient.invalidateQueries({ queryKey: [PLUGIN_ID] })
  const [confirmDisable, setConfirmDisable] = useState(false)
  const [actionError, setActionError] = useState(null)
  const onError = error => setActionError(error instanceof Error ? error.message : String(error))
  const promote = useMutation({ mutationFn: () => api.promote(item.id), onSuccess: invalidate, onError })
  const pin = useMutation({ mutationFn: () => api.pin(item.id, !item.pinned), onSuccess: invalidate, onError })
  const disable = useMutation({
    mutationFn: () => api.disable(item.id),
    onSuccess: () => { setConfirmDisable(false); invalidate() },
    onError
  })
  const anyPending = promote.isPending || pin.isPending || disable.isPending
  return jsxs(Fragment, {
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Button, { variant: 'outline', size: 'sm', disabled: anyPending, onClick: () => { setActionError(null); promote.mutate() }, children: 'Promote' }),
          jsx(Button, { variant: 'outline', size: 'sm', disabled: anyPending, onClick: () => { setActionError(null); pin.mutate() }, children: item.pinned ? 'Unpin' : 'Pin' }),
          jsx(Button, { variant: 'destructive', size: 'sm', disabled: anyPending, onClick: () => { setActionError(null); setConfirmDisable(true) }, children: 'Disable' })
        ]
      }),
      actionError ? jsx('p', { className: 'text-xs', style: { color: 'var(--ui-red)' }, children: `Action failed — ${actionError}` }) : null,
      jsx(ConfirmDialog, {
        open: confirmDisable,
        onClose: () => setConfirmDisable(false),
        onConfirm: () => disable.mutateAsync(),
        title: 'Disable this memory?',
        description: 'Agents will stop recalling it. The memory is kept but hidden from recall.',
        confirmLabel: 'Disable',
        destructive: true
      })
    ]
  })
}

function MemoryDetail({ memoryId, api }) {
  const queryClient = useQueryClient()
  const detailQuery = useQuery({
    queryKey: [PLUGIN_ID, 'detail', memoryId],
    queryFn: () => api.detail(memoryId),
    enabled: Boolean(memoryId)
  })
  if (!memoryId) return null
  if (detailQuery.isLoading) return jsx(Skeleton, { className: 'h-32 w-full' })
  if (detailQuery.error) {
    return jsx(ErrorBox, {
      title: 'Failed to load detail',
      error: detailQuery.error,
      onRetry: () => detailQuery.refetch()
    })
  }
  const data = detailQuery.data
  if (!data || data.error) return jsx(EmptyState, { title: 'Memory not found' })
  const memory = data.memory
  return jsxs(Card, {
    className: 'p-4 space-y-3',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(ScopeBadge, { item: memory }),
              jsx(LifecycleBadge, { lifecycle: memory.lifecycle, freshness: memory.freshness }),
              jsx('span', { className: 'text-xs', style: FAINT, children: memory.id })
            ]
          }),
          jsx(DetailActions, { item: memory, api, queryClient })
        ]
      }),
      jsx('p', { className: 'text-sm whitespace-pre-wrap', children: memory.content }),
      jsx(Separator, {}),
      jsxs('div', {
        className: 'space-y-1',
        children: [
          jsx('h4', { className: 'text-xs font-semibold uppercase', style: MUTED, children: 'Versions' }),
          jsx('div', { className: 'space-y-1', children: (data.versions || []).map(v => jsxs('div', {
            key: v.id,
            className: 'space-y-0.5',
            children: [
              jsxs('div', {
                className: 'text-xs flex items-baseline gap-2',
                children: [
                  jsx('span', { style: FAINT, children: v.id }),
                  jsx(Age, { value: v.created_at }),
                  jsx('span', { className: 'flex-1' }),
                  jsx('span', { style: FAINT, children: v.supersedes ? `supersedes ${v.supersedes}` : null })
                ]
              }),
              jsx('p', { className: 'text-sm', children: v.content })
            ]
          }, v.id)) })
        ]
      }),
      jsxs('div', {
        className: 'space-y-1',
        children: [
          jsx('h4', { className: 'text-xs font-semibold uppercase', style: MUTED, children: 'Evidence' }),
          (data.evidence || []).length
            ? jsx('div', { className: 'space-y-1', children: data.evidence.map(e => jsx('div', {
              className: 'text-xs', style: MUTED,
              children: `${e.kind}: ${e.uri || ''} ${e.note || ''}`
            }, e.id)) })
            : jsx('p', { className: 'text-xs', style: FAINT, children: 'No evidence linked' })
        ]
      }),
      jsxs('div', {
        className: 'space-y-1',
        children: [
          jsx('h4', { className: 'text-xs font-semibold uppercase', style: MUTED, children: 'Audit' }),
          jsx('div', { className: 'space-y-1', children: (data.audit || []).map((a, i) => jsxs('div', {
            className: 'text-xs flex items-baseline gap-2',
            children: [
              jsx(Age, { value: a.created_at }),
              jsx('span', { style: MUTED, children: `${a.actor || 'operator'} · ${a.action}` })
            ]
          }, `${a.created_at}-${i}`)) })
        ]
      })
    ]
  })
}

function MemcorePage({ ctx }) {
  const api = useMemo(() => createMemcoreApi(ctx), [ctx])
  const [view, setView] = useState('candidate')
  const [query, setQuery] = useState('')
  const [openId, setOpenId] = useState(null)
  const projectsQuery = useQuery({ queryKey: [PLUGIN_ID, 'projects'], queryFn: () => api.projects() })
  const stateQuery = useQuery({ queryKey: [PLUGIN_ID, 'state'], queryFn: () => api.state() })
  const counts = stateQuery.data?.counts || {}
  return jsx('div', {
    className: 'mx-auto flex w-full flex-col gap-4 p-4',
    style: { boxSizing: 'border-box', maxWidth: '80rem' },
    children: jsxs(Fragment, {
      children: [
        jsxs('header', {
          className: 'flex items-center gap-2',
          children: [
            jsx('h2', { className: 'text-xl font-semibold', children: 'Shared Project Memory' }),
            jsx(Badge, { variant: 'outline', children: 'memcore' }),
            stateQuery.error ? jsx(ErrorBox, {
              title: 'Failed to load counts',
              error: stateQuery.error,
              onRetry: () => stateQuery.refetch()
            }) : stateQuery.data?.store === null ? jsx('span', {
              className: 'text-xs',
              style: { color: 'var(--ui-orange)' },
              children: 'Memory store not found — lists and actions will be empty.'
            }) : stateQuery.data ? jsx('span', {
              className: 'text-xs', style: FAINT,
              children: Object.entries(counts).map(([k, v]) => `${k}:${v}`).join(' · ')
            }) : null
          ]
        }),
        jsx(Input, {
          placeholder: 'Search shared memory...',
          value: query,
          onChange: e => setQuery(e.target.value)
        }),
        jsxs('div', {
          className: 'flex flex-wrap items-center gap-2',
          children: VIEWS.map(v => jsx(Button, {
            key: v.key,
            variant: view === v.key && !query ? 'default' : 'ghost',
            size: 'sm',
            onClick: () => { setView(v.key); setQuery('') },
            children: v.label
          }, v.key))
        }),
        query.trim()
          ? jsx(SearchResults, { query, api, onOpen: setOpenId })
          : jsx(MemoryList, { view, api, onOpen: setOpenId }),
        jsx(MemoryDetail, { memoryId: openId, api }),
        projectsQuery.data ? jsx('p', {
          className: 'text-xs', style: FAINT,
          children: `projects: ${projectsQuery.data.projects.map(p => p.name).join(', ')}`
        }) : null
      ]
    })
  })
}

function register(ctx) {
  ctx.register({
    id: 'page',
    area: ROUTES_AREA,
    data: { path: PLUGIN_ROUTE },
    render: () => jsx(MemcorePage, { ctx })
  })
  ctx.register({
    id: 'nav',
    area: SIDEBAR_NAV_AREA,
    data: { path: PLUGIN_ROUTE, label: 'MemCore', codicon: 'database' }
  })
  ctx.register({
    id: 'open',
    area: PALETTE_AREA,
    data: {
      id: `${PLUGIN_ID}.open`,
      label: 'Open Shared Project Memory',
      keywords: ['memory', 'memcore', 'shared'],
      run: () => host.navigate(PLUGIN_ROUTE)
    }
  })
}

export default {
  id: PLUGIN_ID,
  name: PLUGIN_NAME,
  version: '0.5.0',
  register
}
