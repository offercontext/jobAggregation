import type { ViewMode } from './navigation';

const VIEW_IDS = new Set<ViewMode>([
  'dashboard',
  'board',
  'applications-list',
  'calendar',
  'reminders',
  'interview',
  'reviews',
  'offers',
  'knowledge',
  'questions',
  'resumes',
  'pilot',
  'settings',
]);

const LEGACY_ALIASES: Readonly<Record<string, ViewMode>> = {
  today: 'dashboard',
  applications: 'board',
  application: 'board',
  list: 'applications-list',
  materials: 'resumes',
  resources: 'resumes',
  stories: 'reviews',
};

const LEGACY_PATHS: Readonly<Record<string, ViewMode>> = {
  '/today': 'dashboard',
  '/dashboard': 'dashboard',
  '/applications': 'board',
  '/applications/board': 'board',
  '/applications/list': 'applications-list',
  '/list': 'applications-list',
  '/calendar': 'calendar',
  '/reminders': 'reminders',
  '/interview': 'interview',
  '/questions': 'questions',
  '/offers': 'offers',
  '/materials': 'resumes',
  '/materials/resumes': 'resumes',
  '/materials/reviews': 'reviews',
  '/materials/knowledge': 'knowledge',
  '/resumes': 'resumes',
  '/reviews': 'reviews',
  '/knowledge': 'knowledge',
  '/pilot': 'pilot',
  '/settings': 'settings',
};

function normalizeView(value: string | null | undefined): ViewMode | undefined {
  if (!value) return undefined;
  let decoded = value;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    return undefined;
  }
  const normalized = decoded.trim().toLowerCase();
  if (normalized in LEGACY_ALIASES) return LEGACY_ALIASES[normalized];
  return VIEW_IDS.has(normalized as ViewMode) ? normalized as ViewMode : undefined;
}

export function readWorkspaceViewFromUrl(input: string | URL): ViewMode {
  const url = input instanceof URL ? input : new URL(input, 'http://localhost');
  const queryView = normalizeView(url.searchParams.get('view'));
  if (queryView) return queryView;

  let rawHash = url.hash.replace(/^#/, '');
  try {
    rawHash = decodeURIComponent(rawHash);
  } catch {
    rawHash = '';
  }
  const hashPath = rawHash.startsWith('/')
    ? (rawHash.length > 1 ? rawHash.replace(/\/$/, '') : rawHash).toLowerCase()
    : undefined;
  if (hashPath && LEGACY_PATHS[hashPath]) return LEGACY_PATHS[hashPath];

  const hashView = normalizeView(rawHash.replace(/^\/?/, '').replace(/^view=/, '').split(/[?&]/, 1)[0]);
  if (hashView) return hashView;

  const path = url.pathname.length > 1 ? url.pathname.replace(/\/$/, '') : url.pathname;
  return LEGACY_PATHS[path.toLowerCase()] ?? 'dashboard';
}

export function readInitialWorkspaceView(): ViewMode {
  if (typeof window === 'undefined') return 'dashboard';
  return readWorkspaceViewFromUrl(window.location.href);
}

export function workspaceViewUrl(view: ViewMode, input: string | URL): string {
  const url = input instanceof URL ? new URL(input) : new URL(input, 'http://localhost');
  url.searchParams.set('view', view);
  return `${url.pathname}${url.search}${url.hash}`;
}

export function pushWorkspaceView(view: ViewMode): void {
  if (typeof window === 'undefined') return;
  const nextUrl = workspaceViewUrl(view, window.location.href);
  const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (nextUrl !== currentUrl) window.history.pushState({ view }, '', nextUrl);
}

export function subscribeToWorkspaceView(onChange: (view: ViewMode) => void): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const sync = () => onChange(readWorkspaceViewFromUrl(window.location.href));
  window.addEventListener('popstate', sync);
  window.addEventListener('hashchange', sync);
  return () => {
    window.removeEventListener('popstate', sync);
    window.removeEventListener('hashchange', sync);
  };
}
