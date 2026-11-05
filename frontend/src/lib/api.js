const BASE = import.meta.env.VITE_API_URL || '';
const DEFAULT_TIMEOUT_MS = 45000;

let _authToken = null;

export function setAuthToken(token) {
  _authToken = token;
}

export function getAuthToken() {
  return _authToken;
}

// Typed errors so callers (useChat) can branch on the failure mode
// without inspecting strings. requestId is attached on every error so
// the UI can surface it for bug reports.

export class ApiTimeoutError extends Error {
  constructor(requestId, timeoutMs) {
    super(`Request timed out after ${timeoutMs}ms`);
    this.name = 'ApiTimeoutError';
    this.requestId = requestId;
    this.timeoutMs = timeoutMs;
  }
}

export class ApiSubscriptionGatedError extends Error {
  constructor(message, requestId, status) {
    super(message);
    this.name = 'ApiSubscriptionGatedError';
    this.requestId = requestId;
    this.status = status;
  }
}

export class ApiNetworkError extends Error {
  constructor(requestId, cause) {
    super(cause && cause.message ? cause.message : 'Network error');
    this.name = 'ApiNetworkError';
    this.requestId = requestId;
    this.cause = cause;
  }
}

function _genRequestId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // Defensive fallback for environments without crypto.randomUUID
  // (older Safari, some test runners). Not cryptographically strong;
  // only used for log correlation.
  return 'rid-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

async function request(method, path, body, opts = {}) {
  const timeoutMs = typeof opts.timeoutMs === 'number' ? opts.timeoutMs : DEFAULT_TIMEOUT_MS;
  const requestId = _genRequestId();
  const controller = new AbortController();
  const timeoutHandle = setTimeout(() => controller.abort(), timeoutMs);

  const fetchOpts = {
    method,
    credentials: 'include',
    headers: { 'X-Request-Id': requestId },
    signal: controller.signal,
  };
  if (body !== undefined) {
    fetchOpts.headers['Content-Type'] = 'application/json';
    fetchOpts.body = JSON.stringify(body);
  }
  if (_authToken) {
    fetchOpts.headers['Authorization'] = `Bearer ${_authToken}`;
  }

  let res;
  try {
    res = await fetch(BASE + path, fetchOpts);
  } catch (err) {
    if (err && err.name === 'AbortError') {
      throw new ApiTimeoutError(requestId, timeoutMs);
    }
    throw new ApiNetworkError(requestId, err);
  } finally {
    clearTimeout(timeoutHandle);
  }

  if (res.status === 401) {
    // Keep the message string 'Unauthorized' so existing callers
    // (AppLayout subscription gate) that match on err.message keep
    // working. The new requestId is attached as a property for
    // callers that want to surface it.
    const e = new Error('Unauthorized');
    e.requestId = requestId;
    throw e;
  }
  if (res.status === 402) {
    let parsed = {};
    try {
      parsed = await res.json();
    } catch (_) {
      parsed = {};
    }
    // Wake the existing subscription-status re-check channel before
    // throwing. AppLayout listens for this event and calls
    // checkSubscription(), which flips the gated modal via the existing
    // code path. Defense-in-depth: api.js is the active-action layer,
    // the focus-triggered re-check is the background-liveness layer.
    window.dispatchEvent(new CustomEvent('subscription:gated-detected', {
      detail: { status: parsed.status || 'unknown', requestId },
    }));
    throw new ApiSubscriptionGatedError(
      parsed.error || 'subscription_required',
      requestId,
      parsed.status || 'unknown',
    );
  }
  // Expose requestId on the response so callers can correlate even
  // when the body fails to parse. The server also echoes it as the
  // X-Request-Id response header.
  try { Object.defineProperty(res, 'requestId', { value: requestId }); } catch { /* ignore */ }
  return res;
}

const api = {
  get:  (path, opts = {})       => request('GET',  path, undefined, opts),
  post: (path, body, opts = {}) => request('POST', path, body, opts),
};

export default api;
