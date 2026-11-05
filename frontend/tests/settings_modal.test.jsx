/**
 * Regression guards for A.1 Commit 6 — SettingsModal component.
 *
 * Covers:
 *   - Initial GET + state population
 *   - Three toggle state flows (State 1 / 2 / 3)
 *   - postMessage listener (accept same-origin + google, reject
 *     wrong origin, reject wrong type)
 *   - Three dismissal paths (X button, backdrop click, Escape key)
 *   - Edge cases: popup blocked, 500ms 409-retry race, 30s
 *     abandonment timeout, partial-scope error after double 409,
 *     oauth_complete cancels the 30s timeout (happy path)
 *
 * Mocking pattern mirrors tests/postmessage_listener.test.jsx
 * (vi.mock for Clerk + api, new MessageEvent dispatch through
 * act()) and tests/user_menu_subscription.test.jsx (api.post.
 * mockResolvedValue, vi.fn() for injected deps).
 */

import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import SettingsModal from '../src/components/settings/SettingsModal';

// Stub Clerk's hooks. useUser is legacy; useClerk supplies signOut for the
// delete-my-data flow (the destructive confirm signs the user out).
vi.mock('@clerk/clerk-react', () => ({
  useUser: () => ({ user: { id: 'user_test_123' } }),
  useClerk: () => ({ signOut: () => {} }),
}));

// Stub the api module
vi.mock('../src/lib/api', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));
import api from '../src/lib/api';


// ── Helpers ─────────────────────────────────────────────────────────────

function makeResponse({ ok, status, body }) {
  return { ok, status, json: () => Promise.resolve(body ?? {}) };
}

function dispatchOAuthMessage({ origin = window.location.origin, data }) {
  const event = new MessageEvent('message', { origin, data, source: window });
  act(() => { window.dispatchEvent(event); });
}

function renderOpen(onClose = () => {}) {
  return render(<SettingsModal isOpen={true} onClose={onClose} />);
}

// Helper: after mount, wait for the initial GET to resolve and the loading
// placeholder to disappear. Tests that need the "main" view can call this
// before interacting with the toggle.
async function waitForLoaded() {
  await waitFor(() => {
    expect(screen.queryByText('Loading…')).toBeNull();
  });
}


// ── Per-test resets ─────────────────────────────────────────────────────

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
  // Default GET: initial load returns disabled + no scope. Individual
  // tests override before render.
  api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
    enabled: false, scope_granted: false,
  } }));
});


// ── Tests ───────────────────────────────────────────────────────────────

describe('SettingsModal — initial load', () => {
  it('test_initial_get_populates_toggle_state', async () => {
    api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: true, scope_granted: true,
    } }));
    renderOpen();

    await waitForLoaded();
    expect(api.get).toHaveBeenCalledWith('/app/settings/google-actions');
    const toggle = screen.getByRole('checkbox');
    expect(toggle.checked).toBe(true);
  });

  it('test_main_view_section_order_connections_then_actions', async () => {
    api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: false, scope_granted: false,
    } }));
    renderOpen();
    await waitForLoaded();

    // Section headers present; the Actions toggle block is no longer under
    // a "Google" header.
    const connections = screen.getByRole('heading', { name: 'Connections' });
    const actions = screen.getByRole('heading', { name: 'Actions' });
    expect(connections).toBeInTheDocument();
    expect(actions).toBeInTheDocument();
    // Connections renders before Actions in DOM order.
    expect(connections.compareDocumentPosition(actions) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
  });
});

describe('SettingsModal — three state flows', () => {
  it('test_state_2_toggle_on_with_scope_posts_and_stays_on', async () => {
    api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: false, scope_granted: true,
    } }));
    api.post.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: true, scope_granted: true,
    } }));
    renderOpen();
    await waitForLoaded();

    const toggle = screen.getByRole('checkbox');
    await act(async () => { fireEvent.click(toggle); });

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/app/settings/google-actions', { enabled: true });
    });
    expect(toggle.checked).toBe(true);
    // Still in main view — no intermediate shown
    expect(screen.queryByText('Continue to Google')).toBeNull();
  });

  it('test_state_1_toggle_on_without_scope_shows_intermediate', async () => {
    // Default beforeEach GET response: enabled=false, scope_granted=false
    renderOpen();
    await waitForLoaded();

    const toggle = screen.getByRole('checkbox');
    await act(async () => { fireEvent.click(toggle); });

    // No POST yet — scope missing → intermediate view
    expect(api.post).not.toHaveBeenCalled();
    expect(screen.getByText('Continue to Google')).toBeInTheDocument();
    expect(screen.getByText('Cancel')).toBeInTheDocument();
  });

  it('test_state_3_toggle_off_posts_and_persists', async () => {
    api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: true, scope_granted: true,
    } }));
    api.post.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: false, scope_granted: true,
    } }));
    renderOpen();
    await waitForLoaded();

    const toggle = screen.getByRole('checkbox');
    expect(toggle.checked).toBe(true);
    await act(async () => { fireEvent.click(toggle); });

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/app/settings/google-actions', { enabled: false });
    });
    expect(toggle.checked).toBe(false);
  });
});

describe('SettingsModal — postMessage listener', () => {
  it('test_postmessage_same_origin_google_triggers_retry_post', async () => {
    // Full write-grant flow: intermediate -> Continue (sets the write-grant
    // pending flag) -> oauth_complete. The listener acts only when a write
    // grant is pending, so the Continue click is load-bearing now.
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => ({ closed: false }));
    api.post.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: true, scope_granted: true,
    } }));
    renderOpen();
    await waitForLoaded();

    // Walk into intermediate view (so the flow context matches reality)
    await act(async () => { fireEvent.click(screen.getByRole('checkbox')); });
    expect(screen.getByText('Continue to Google')).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByText('Continue to Google')); });

    // Simulate OAuth callback posting the completion message
    dispatchOAuthMessage({
      data: { type: 'oauth_complete', provider: 'google' },
    });

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/app/settings/google-actions', { enabled: true });
    });
    // View returns to main, toggle ON
    await waitFor(() => {
      expect(screen.queryByText('Continue to Google')).toBeNull();
    });
    expect(screen.getByRole('checkbox').checked).toBe(true);
    openSpy.mockRestore();
  });

  it('test_postmessage_rejects_wrong_origin', async () => {
    renderOpen();
    await waitForLoaded();
    await act(async () => { fireEvent.click(screen.getByRole('checkbox')); });

    dispatchOAuthMessage({
      origin: 'https://evil.example.com',
      data: { type: 'oauth_complete', provider: 'google' },
    });

    // No POST fired — origin check rejected
    expect(api.post).not.toHaveBeenCalled();
  });

  it('test_postmessage_rejects_wrong_type', async () => {
    renderOpen();
    await waitForLoaded();
    await act(async () => { fireEvent.click(screen.getByRole('checkbox')); });

    dispatchOAuthMessage({
      data: { type: 'something_else', provider: 'google' },
    });

    expect(api.post).not.toHaveBeenCalled();
  });

  it('test_oauth_complete_ignored_when_no_write_grant_pending', async () => {
    // Read-scope reconnect from the Connections section dispatches the same
    // google oauth_complete. Without a write grant pending, the modal must
    // NOT POST to enable Actions.
    api.post.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {} }));
    renderOpen();
    await waitForLoaded();
    // No Continue click — writeGrantPendingRef stays false.

    dispatchOAuthMessage({ data: { type: 'oauth_complete', provider: 'google' } });
    await act(async () => { await Promise.resolve(); });

    expect(api.post).not.toHaveBeenCalled();
  });

  it('test_oauth_canceled_ignored_when_no_write_grant_pending', async () => {
    // A canceled read-scope reconnect must not flip the Actions toggle off.
    api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: true, scope_granted: true,
    } }));
    renderOpen();
    await waitForLoaded();
    const toggle = screen.getByRole('checkbox');
    expect(toggle.checked).toBe(true);

    dispatchOAuthMessage({ data: { type: 'oauth_canceled', provider: 'google' } });
    await act(async () => { await Promise.resolve(); });

    // Toggle untouched, no cancel banner shown
    expect(screen.getByRole('checkbox').checked).toBe(true);
    expect(screen.queryByText(/Connection canceled/)).toBeNull();
  });
});

describe('SettingsModal — dismissal paths', () => {
  it('test_dismissal_x_button_calls_onClose', async () => {
    const onClose = vi.fn();
    render(<SettingsModal isOpen={true} onClose={onClose} />);
    await waitForLoaded();

    const closeBtn = screen.getByLabelText('Close settings');
    await act(async () => { fireEvent.click(closeBtn); });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('test_dismissal_backdrop_click_calls_onClose', async () => {
    const onClose = vi.fn();
    const { container } = render(<SettingsModal isOpen={true} onClose={onClose} />);
    await waitForLoaded();

    // Click on overlay (outermost div); the card's onClick handler doesn't
    // exist, so backdrop click bubbles through only when e.target === e.currentTarget.
    const overlay = container.firstChild; // overlay div
    await act(async () => { fireEvent.click(overlay); });
    expect(onClose).toHaveBeenCalledTimes(1);

    // Clicking on card body (heading) must NOT trigger close
    onClose.mockClear();
    await act(async () => { fireEvent.click(screen.getByText('Settings')); });
    expect(onClose).not.toHaveBeenCalled();
  });

  it('test_dismissal_escape_key_calls_onClose', async () => {
    const onClose = vi.fn();
    render(<SettingsModal isOpen={true} onClose={onClose} />);
    await waitForLoaded();

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('SettingsModal — popup + timeout + retry edge cases', () => {
  afterEach(() => { vi.useRealTimers(); });

  it('test_popup_blocked_shows_error_view', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    renderOpen();
    await waitForLoaded();

    // Flip toggle ON → intermediate
    await act(async () => { fireEvent.click(screen.getByRole('checkbox')); });
    // Click Continue → window.open returns null → error view
    await act(async () => { fireEvent.click(screen.getByText('Continue to Google')); });

    expect(openSpy).toHaveBeenCalled();
    expect(screen.getByText(/browser blocked the Google popup/)).toBeInTheDocument();
    openSpy.mockRestore();
  });

  it('test_500ms_retry_race_first_409_retries_second_200_succeeds', async () => {
    vi.useFakeTimers();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => ({ closed: false }));
    // First POST returns 409 (OAuth callback DB commit still racing),
    // second POST returns 200.
    api.post
      .mockResolvedValueOnce(makeResponse({ ok: false, status: 409, body: { error: 'scope_required' } }))
      .mockResolvedValueOnce(makeResponse({ ok: true, status: 200, body: { enabled: true, scope_granted: true } }));

    renderOpen();
    // Let the initial GET's promise resolve with fake timers
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { fireEvent.click(screen.getByRole('checkbox')); });
    // Continue sets the write-grant pending flag the listener gates on.
    await act(async () => { fireEvent.click(screen.getByText('Continue to Google')); });

    // Dispatch oauth_complete to trigger attemptEnable
    dispatchOAuthMessage({ data: { type: 'oauth_complete', provider: 'google' } });
    // First POST resolves (409)
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    // Advance 500ms — second POST fires and resolves (200)
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });

    expect(api.post).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('checkbox').checked).toBe(true);
    expect(screen.queryByText('Continue to Google')).toBeNull();
    openSpy.mockRestore();
  });

  it('test_30s_popup_abandonment_reverts_toggle', async () => {
    vi.useFakeTimers();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => ({
      closed: false,
    }));

    renderOpen();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { fireEvent.click(screen.getByRole('checkbox')); });
    // Intermediate visible
    await act(async () => { fireEvent.click(screen.getByText('Continue to Google')); });

    // popup opened, no oauth_complete received — advance 30s
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });

    // Toggle reverted, view back to main (no Continue button)
    expect(screen.getByRole('checkbox').checked).toBe(false);
    expect(screen.queryByText('Continue to Google')).toBeNull();
    // No POST ever fired — user never completed auth
    expect(api.post).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('test_partial_scope_error_after_both_posts_return_409', async () => {
    vi.useFakeTimers();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => ({ closed: false }));
    api.post
      .mockResolvedValueOnce(makeResponse({ ok: false, status: 409, body: {} }))
      .mockResolvedValueOnce(makeResponse({ ok: false, status: 409, body: {} }));

    renderOpen();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { fireEvent.click(screen.getByRole('checkbox')); });
    await act(async () => { fireEvent.click(screen.getByText('Continue to Google')); });

    dispatchOAuthMessage({ data: { type: 'oauth_complete', provider: 'google' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    // Retry kicks in after 500ms
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });

    expect(api.post).toHaveBeenCalledTimes(2);
    // Partial-scope error view shown
    expect(screen.getByText(/didn't grant full permission/)).toBeInTheDocument();
    // Toggle reverted — partial_scope_error view does not render the checkbox
    expect(screen.queryByRole('checkbox')).toBeNull();
    openSpy.mockRestore();
  });

  it('test_oauth_complete_cancels_30s_abandonment_timeout', async () => {
    vi.useFakeTimers();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => ({
      closed: false,
    }));
    api.post.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: true, scope_granted: true,
    } }));

    renderOpen();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { fireEvent.click(screen.getByRole('checkbox')); });
    await act(async () => { fireEvent.click(screen.getByText('Continue to Google')); });

    // Advance 5s — timeout still pending
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });

    // oauth_complete arrives before 30s elapses
    dispatchOAuthMessage({ data: { type: 'oauth_complete', provider: 'google' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });

    expect(api.post).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('checkbox').checked).toBe(true);

    // Advance past the original 30s mark — timeout should have been cancelled
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });

    // Toggle STILL ON, view STILL main (timeout did not fire)
    expect(screen.getByRole('checkbox').checked).toBe(true);
    expect(screen.queryByText('Continue to Google')).toBeNull();
    expect(api.post).toHaveBeenCalledTimes(1);  // no retry needed
    openSpy.mockRestore();
  });
});

describe('SettingsModal — session-expired error handling', () => {
  // Test 5 stubs window.location.reload by redefining window.location
  // itself — jsdom marks Location.prototype.reload as non-configurable,
  // so we can't swap just the reload property. Redefining the whole
  // location on window is the reliable path. Save and restore every
  // test so the mock can't leak into sibling tests (including the
  // popup+timeout block above, which runs in the same file/suite
  // context and reads window.location.origin).
  const originalLocation = window.location;
  afterEach(() => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: originalLocation,
    });
  });

  it('test_initial_get_401_shows_session_expired_view', async () => {
    api.get.mockReset();
    api.get.mockRejectedValue(new Error('Unauthorized'));
    renderOpen();

    await waitFor(() => {
      expect(screen.getByText(/session has expired/i)).toBeInTheDocument();
    });
    expect(screen.getByText('Refresh')).toBeInTheDocument();
    // Session-expired view replaces main view — no toggle rendered
    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('test_disable_post_401_reverts_and_shows_session_expired', async () => {
    api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: true, scope_granted: true,
    } }));
    api.post.mockRejectedValue(new Error('Unauthorized'));
    renderOpen();
    await waitForLoaded();

    const toggle = screen.getByRole('checkbox');
    expect(toggle.checked).toBe(true);
    await act(async () => { fireEvent.click(toggle); });

    await waitFor(() => {
      expect(screen.getByText(/session has expired/i)).toBeInTheDocument();
    });
    expect(api.post).toHaveBeenCalledWith('/app/settings/google-actions', { enabled: false });
  });

  it('test_enable_post_401_reverts_and_shows_session_expired', async () => {
    // scope_granted=true → toggle ON takes direct-enable path (State 2)
    api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: false, scope_granted: true,
    } }));
    api.post.mockRejectedValue(new Error('Unauthorized'));
    renderOpen();
    await waitForLoaded();

    const toggle = screen.getByRole('checkbox');
    expect(toggle.checked).toBe(false);
    await act(async () => { fireEvent.click(toggle); });

    await waitFor(() => {
      expect(screen.getByText(/session has expired/i)).toBeInTheDocument();
    });
    expect(api.post).toHaveBeenCalledWith('/app/settings/google-actions', { enabled: true });
  });

  it('test_oauth_complete_retry_401_shows_session_expired', async () => {
    // scope_granted=false → toggle ON takes intermediate path; then
    // oauth_complete triggers attemptEnable() which POSTs and 401s.
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => ({ closed: false }));
    api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      enabled: false, scope_granted: false,
    } }));
    api.post.mockRejectedValue(new Error('Unauthorized'));
    renderOpen();
    await waitForLoaded();

    await act(async () => { fireEvent.click(screen.getByRole('checkbox')); });
    expect(screen.getByText('Continue to Google')).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByText('Continue to Google')); });

    dispatchOAuthMessage({ data: { type: 'oauth_complete', provider: 'google' } });

    await waitFor(() => {
      expect(screen.getByText(/session has expired/i)).toBeInTheDocument();
    });
    expect(api.post).toHaveBeenCalledWith('/app/settings/google-actions', { enabled: true });
    openSpy.mockRestore();
  });

  it('test_refresh_button_calls_window_location_reload', async () => {
    const reloadMock = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...originalLocation, reload: reloadMock },
    });

    api.get.mockReset();
    api.get.mockRejectedValue(new Error('Unauthorized'));
    renderOpen();

    await waitFor(() => {
      expect(screen.getByText('Refresh')).toBeInTheDocument();
    });
    await act(async () => { fireEvent.click(screen.getByText('Refresh')); });

    expect(reloadMock).toHaveBeenCalledTimes(1);
  });
});
