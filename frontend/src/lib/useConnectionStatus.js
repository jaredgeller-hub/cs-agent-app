import { useCallback, useEffect, useState } from 'react';
import api from './api';

// G.2 Stage 2: shared source of truth for /app/connection-status.
// Banner + dots both consume this hook — eliminates parallel polling
// and the banner-vs-dots race where two independent fetches could
// briefly disagree about provider state.
//
// Wire shape: a flat dict {gmail, calendar, hubspot, gainsight}, value
// in "ok"|"expired"|"missing"|"error". `last_verified_at` is kept
// server-side (used internally for the stale-fallback path) and is
// intentionally NOT exposed on the wire. Hook returns the dict as-is;
// aggregation (gmail+calendar → "Google" in copy) lives in the banner.
//
// Refresh triggers:
//   1. Mount
//   2. window 'focus' (user returns to tab)
//   3. window 'message' with event.data.type === 'oauth_complete' AND
//      event.origin === window.location.origin (popup OAuth completed).
//      The existing OAuth callback HTML in main.py dispatches this
//      exact shape — the hook reuses it rather than introducing a new
//      CustomEvent layer.
//
// Origin validation mirrors OnboardingWizard.jsx + SettingsModal.jsx
// listeners — protects against hostile postMessage from third-party
// iframes / windows attempting to force a refetch.
//
// Dedup is module-level — two hook instances (banner + dots) mounted
// at the same time share one fetch. A per-instance dedup wouldn't
// help here: both instances mount in the same tick, so each one's
// "first" trigger lands before the other has a chance to record its
// timestamp. The shared dedup window collapses both first-tick
// triggers into one fetch and broadcasts the result back to all
// subscribers.

const DEDUP_WINDOW_MS = 1000;

// ── Module-level shared store ────────────────────────────────────────────────
let _cachedProviders = null;
let _lastFetchTime = 0;
let _inFlightPromise = null;
const _providersSubscribers = new Set();
const _loadingSubscribers = new Set();
const _errorSubscribers = new Set();

function _emit(subscribers, value) {
  for (const cb of subscribers) {
    try { cb(value); } catch { /* ignore subscriber errors */ }
  }
}

function _startFetch() {
  if (_inFlightPromise) return _inFlightPromise;
  _lastFetchTime = Date.now();
  _emit(_loadingSubscribers, true);
  _emit(_errorSubscribers, null);
  _inFlightPromise = (async () => {
    try {
      const res = await api.get('/app/connection-status');
      const data = await res?.json();
      if (data && !data.error) {
        _cachedProviders = data;
        _emit(_providersSubscribers, data);
      }
      return data;
    } catch (err) {
      _emit(_errorSubscribers, err);
      throw err;
    } finally {
      _inFlightPromise = null;
      _emit(_loadingSubscribers, false);
    }
  })();
  // Swallow unhandled rejections — subscribers already received the error.
  _inFlightPromise.catch(() => {});
  return _inFlightPromise;
}

function _maybeFetch({ force = false } = {}) {
  if (force) return _startFetch();
  if (_inFlightPromise) return _inFlightPromise;
  if (Date.now() - _lastFetchTime < DEDUP_WINDOW_MS) return null;
  return _startFetch();
}

// Test-only escape hatch. Production code never imports this.
export function __resetForTests() {
  _cachedProviders = null;
  _lastFetchTime = 0;
  _inFlightPromise = null;
  _providersSubscribers.clear();
  _loadingSubscribers.clear();
  _errorSubscribers.clear();
}

export default function useConnectionStatus() {
  const [providers, setProviders] = useState(_cachedProviders);
  const [isLoading, setIsLoading] = useState(!!_inFlightPromise);
  const [error, setError] = useState(null);

  useEffect(() => {
    _providersSubscribers.add(setProviders);
    _loadingSubscribers.add(setIsLoading);
    _errorSubscribers.add(setError);

    _maybeFetch();

    function onFocus() { _maybeFetch(); }
    function onMessage(event) {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== 'oauth_complete') return;
      _maybeFetch();
    }

    window.addEventListener('focus', onFocus);
    window.addEventListener('message', onMessage);
    return () => {
      _providersSubscribers.delete(setProviders);
      _loadingSubscribers.delete(setIsLoading);
      _errorSubscribers.delete(setError);
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('message', onMessage);
    };
  }, []);

  const refetch = useCallback(() => _maybeFetch({ force: true }), []);

  return { providers, isLoading, error, refetch };
}
