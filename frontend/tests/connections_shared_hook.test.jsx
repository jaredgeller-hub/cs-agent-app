/**
 * G.2 Stage 2 commit 2: pins that ConnectionExpiryBanner and
 * ConnectionStatus consume useConnectionStatus as their single source
 * of truth.
 *
 * Two layers of tests:
 *
 *   1. Hook-mocked unit tests — mock useConnectionStatus and assert
 *      the components render the right thing for each providers shape.
 *      Decouples component rendering from the hook's network/event
 *      logic (which has its own test file).
 *
 *   2. Integration dedup test — mount both components together with
 *      api.get mocked, assert exactly one /app/connection-status call
 *      lands (proves the parallel-polling problem is gone).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';

// ── 1. Hook-mocked unit tests ────────────────────────────────────────────────

vi.mock('../src/lib/useConnectionStatus', () => ({
  default: vi.fn(),
}));

import useConnectionStatus from '../src/lib/useConnectionStatus';
import {
  getExpiredProviderKeys,
  formatExpiryMessage,
} from '../src/lib/connectionExpiry';
import ConnectionStatus from '../src/components/connections/ConnectionStatus';
import ConnectionExpiryBanner from '../src/components/connections/ConnectionExpiryBanner';

function setHookValue(providers, overrides = {}) {
  useConnectionStatus.mockReturnValue({
    providers,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
    ...overrides,
  });
}

describe('Banner consumes useConnectionStatus', () => {
  beforeEach(() => {
    useConnectionStatus.mockReset();
    try { sessionStorage.clear(); } catch { /* ignore */ }
  });
  afterEach(() => { cleanup(); });

  it('renders when hook reports an expired provider', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    const banner = screen.getByRole('alert');
    expect(banner).toBeInTheDocument();
    // Phase 3 a11y: aria-live="polite" on the container.
    expect(banner.getAttribute('aria-live')).toBe('polite');
  });

  it('does NOT render when hook reports all-ok', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    const { container } = render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it('does NOT render when hook providers is null (still loading)', () => {
    setHookValue(null);
    const { container } = render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});

describe('Dots consume useConnectionStatus', () => {
  beforeEach(() => { useConnectionStatus.mockReset(); });
  afterEach(() => { cleanup(); });

  it('renders Gmail + Calendar + HubSpot with correct labels', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    render(<ConnectionStatus />);
    expect(screen.getByText('Gmail')).toBeInTheDocument();
    expect(screen.getByText('Calendar')).toBeInTheDocument();
    expect(screen.getByText('HubSpot')).toBeInTheDocument();
  });

  it('renders nothing when hook providers is null', () => {
    setHookValue(null);
    const { container } = render(<ConnectionStatus />);
    expect(container.firstChild).toBeNull();
  });
});

describe('Banner + dots agree on state (regression: parallel-fetch race)', () => {
  beforeEach(() => { useConnectionStatus.mockReset(); });
  afterEach(() => { cleanup(); });

  const cases = [
    { gmail: 'ok',      calendar: 'ok',      hubspot: 'ok',      gainsight: 'missing', bannerShown: false },
    { gmail: 'expired', calendar: 'ok',      hubspot: 'ok',      gainsight: 'missing', bannerShown: true },
    { gmail: 'ok',      calendar: 'expired', hubspot: 'ok',      gainsight: 'missing', bannerShown: true },
    { gmail: 'ok',      calendar: 'ok',      hubspot: 'expired', gainsight: 'missing', bannerShown: true },
    { gmail: 'ok',      calendar: 'ok',      hubspot: 'missing', gainsight: 'expired', bannerShown: true },
    { gmail: 'missing', calendar: 'missing', hubspot: 'missing', gainsight: 'missing', bannerShown: false },
    { gmail: 'error',   calendar: 'error',   hubspot: 'error',   gainsight: 'error',   bannerShown: false },
  ];

  for (const tc of cases) {
    const { gmail, calendar, hubspot, gainsight, bannerShown } = tc;
    it(`gmail=${gmail} calendar=${calendar} hubspot=${hubspot} gainsight=${gainsight} -> banner ${bannerShown ? 'shown' : 'hidden'}`, () => {
      setHookValue({ gmail, calendar, hubspot, gainsight });
      // Both components see the same hook value -> they cannot disagree.
      const { container } = render(
        <>
          <ConnectionExpiryBanner onOpenSettings={() => {}} />
          <ConnectionStatus />
        </>
      );
      const banner = container.querySelector('[role="alert"]');
      if (bannerShown) {
        expect(banner).not.toBeNull();
      } else {
        expect(banner).toBeNull();
      }
      // Dots always render Gmail + Calendar (when providers present).
      expect(screen.getByText('Gmail')).toBeInTheDocument();
      expect(screen.getByText('Calendar')).toBeInTheDocument();
    });
  }
});

// ── 2. Integration dedup test ────────────────────────────────────────────────
//
// Stops mocking useConnectionStatus; uses the real hook with api.get
// mocked. Mounts both components together and asserts exactly one
// /app/connection-status fetch lands. This is the load-bearing test
// for "no more parallel polling."

describe('Network dedup: banner + dots share a single fetch', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doUnmock('../src/lib/useConnectionStatus');
  });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it('mounting banner + dots together results in ONE /app/connection-status call', async () => {
    // Re-import after unmocking so we get the real hook.
    vi.doMock('../src/lib/api', () => ({
      default: { get: vi.fn(), post: vi.fn() },
    }));
    const apiModule = await import('../src/lib/api');
    const realHookModule = await import('../src/lib/useConnectionStatus');
    const bannerModule = await import('../src/components/connections/ConnectionExpiryBanner');
    const dotsModule = await import('../src/components/connections/ConnectionStatus');

    apiModule.default.get.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing',
      }),
    });

    // Sanity: real hook is imported, not the mock above.
    expect(typeof realHookModule.default).toBe('function');

    const Banner = bannerModule.default;
    const Dots = dotsModule.default;

    render(
      <>
        <Banner onOpenSettings={() => {}} />
        <Dots />
      </>
    );

    await waitFor(() => expect(apiModule.default.get).toHaveBeenCalled());
    // Allow microtasks + any late triggers to settle.
    await new Promise((r) => setTimeout(r, 50));

    const callsToConnectionStatus = apiModule.default.get.mock.calls.filter(
      (c) => c[0] === '/app/connection-status'
    );
    expect(callsToConnectionStatus.length).toBe(1);
  });
});

// ── 3. Phase 3: banner copy + per-provider naming ────────────────────────────

describe('getExpiredProviderKeys (Google aggregation rule)', () => {
  // Rule: Google is expired if ANY of gmail/calendar shows expired.
  // One OAuth grant covers both, so a sub-provider expiry means the
  // grant itself is compromised — collapsing to "Google" matches what
  // the user can actually do (one /auth/google/start popup).

  it('returns [] when no providers expired', () => {
    expect(getExpiredProviderKeys({
      gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing',
    })).toEqual([]);
  });

  it('returns ["google"] when gmail expired but calendar ok (any-expired rule)', () => {
    expect(getExpiredProviderKeys({
      gmail: 'expired', calendar: 'ok', hubspot: 'ok', gainsight: 'missing',
    })).toEqual(['google']);
  });

  it('returns ["google"] when calendar expired but gmail ok (any-expired rule)', () => {
    expect(getExpiredProviderKeys({
      gmail: 'ok', calendar: 'expired', hubspot: 'ok', gainsight: 'missing',
    })).toEqual(['google']);
  });

  it('returns ["google"] (not two entries) when BOTH gmail and calendar expired', () => {
    expect(getExpiredProviderKeys({
      gmail: 'expired', calendar: 'expired', hubspot: 'ok', gainsight: 'missing',
    })).toEqual(['google']);
  });

  it('returns ["google"] when gmail expired and calendar missing (any-expired wins over missing)', () => {
    expect(getExpiredProviderKeys({
      gmail: 'expired', calendar: 'missing', hubspot: 'ok', gainsight: 'missing',
    })).toEqual(['google']);
  });

  it('returns ["hubspot"] when only hubspot expired', () => {
    expect(getExpiredProviderKeys({
      gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing',
    })).toEqual(['hubspot']);
  });

  it('returns ["google", "hubspot"] in stable order', () => {
    expect(getExpiredProviderKeys({
      gmail: 'expired', calendar: 'ok', hubspot: 'expired', gainsight: 'missing',
    })).toEqual(['google', 'hubspot']);
  });

  it('returns ["google", "hubspot", "gainsight"] in stable order', () => {
    expect(getExpiredProviderKeys({
      gmail: 'expired', calendar: 'expired', hubspot: 'expired', gainsight: 'expired',
    })).toEqual(['google', 'hubspot', 'gainsight']);
  });

  it('returns [] when providers is null (still loading)', () => {
    expect(getExpiredProviderKeys(null)).toEqual([]);
  });
});

describe('formatExpiryMessage (grammar)', () => {
  it('single provider: "Your X connection has expired."', () => {
    expect(formatExpiryMessage(['google']))
      .toBe('Your Google connection has expired.');
  });

  it('two providers: "Your A and B connections have expired."', () => {
    expect(formatExpiryMessage(['google', 'hubspot']))
      .toBe('Your Google and HubSpot connections have expired.');
  });

  it('three providers: Oxford comma "Your A, B, and C connections have expired."', () => {
    expect(formatExpiryMessage(['google', 'hubspot', 'gainsight']))
      .toBe('Your Google, HubSpot, and Gainsight connections have expired.');
  });

  it('uses proper-noun casing (Google / HubSpot / Gainsight, not lowercase)', () => {
    const msg = formatExpiryMessage(['google', 'hubspot', 'gainsight']);
    expect(msg).toContain('Google');
    expect(msg).toContain('HubSpot');
    expect(msg).toContain('Gainsight');
    expect(msg).not.toContain('google');
    expect(msg).not.toContain('hubspot');
    expect(msg).not.toContain('gainsight');
  });

  it('empty list returns empty string', () => {
    expect(formatExpiryMessage([])).toBe('');
  });
});

describe('Banner renders Phase 3 copy end-to-end', () => {
  beforeEach(() => {
    useConnectionStatus.mockReset();
    try { sessionStorage.clear(); } catch { /* ignore */ }
  });
  afterEach(() => { cleanup(); });

  it('single-provider copy: HubSpot only expired', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.getByText(/Your HubSpot connection has expired\./)).toBeInTheDocument();
  });

  it('single-provider copy: Google (gmail expired, calendar ok) aggregates to "Google" — message says it once, plus one reconnect button', () => {
    setHookValue({ gmail: 'expired', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.getByText(/Your Google connection has expired\./)).toBeInTheDocument();
    // The aggregation guarantee: message + button = 2 mentions total
    // (one Google mention in the prose, one in the "Reconnect Google"
    // button label). Critically NOT 3+ — gmail/calendar do not each
    // get their own message line or their own button.
    const banner = screen.getByRole('alert');
    const googleMatches = (banner.textContent.match(/Google/g) || []).length;
    expect(googleMatches).toBe(2);
    // And the single reconnect button is "Reconnect Google".
    expect(screen.getByText('Reconnect Google')).toBeInTheDocument();
    expect(screen.queryByText('Reconnect Gmail')).toBeNull();
    expect(screen.queryByText('Reconnect Calendar')).toBeNull();
  });

  it('two-provider copy: Google + HubSpot', () => {
    setHookValue({ gmail: 'expired', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.getByText(/Your Google and HubSpot connections have expired\./)).toBeInTheDocument();
  });

  it('three-provider copy with Oxford comma', () => {
    setHookValue({ gmail: 'expired', calendar: 'expired', hubspot: 'expired', gainsight: 'expired' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.getByText(/Your Google, HubSpot, and Gainsight connections have expired\./)).toBeInTheDocument();
  });

  it('does NOT say "one or more" (legacy copy)', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    const { container } = render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(container.textContent).not.toMatch(/one or more/i);
  });
});

// ── 4. Phase 4: direct per-provider reconnect buttons ────────────────────────

describe('Banner direct-reconnect buttons (Phase 4)', () => {
  let windowOpenSpy;

  beforeEach(() => {
    useConnectionStatus.mockReset();
    try { sessionStorage.clear(); } catch { /* ignore */ }
    windowOpenSpy = vi.spyOn(window, 'open').mockReturnValue({ closed: false });
  });
  afterEach(() => {
    cleanup();
    windowOpenSpy.mockRestore();
  });

  it('renders one reconnect button per expired provider', () => {
    setHookValue({ gmail: 'expired', calendar: 'ok', hubspot: 'expired', gainsight: 'expired' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.getByText('Reconnect Google')).toBeInTheDocument();
    expect(screen.getByText('Reconnect HubSpot')).toBeInTheDocument();
    expect(screen.getByText('Reconnect Gainsight')).toBeInTheDocument();
  });

  it('"Reconnect Google" opens /auth/google/start in a popup', () => {
    setHookValue({ gmail: 'expired', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    screen.getByText('Reconnect Google').click();
    expect(windowOpenSpy).toHaveBeenCalledWith(
      '/auth/google/start', '_blank', 'width=500,height=600,popup=yes'
    );
  });

  it('"Reconnect HubSpot" opens /auth/hubspot/start in a popup', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    screen.getByText('Reconnect HubSpot').click();
    expect(windowOpenSpy).toHaveBeenCalledWith(
      '/auth/hubspot/start', '_blank', 'width=500,height=600,popup=yes'
    );
  });

  it('"Reconnect Gainsight" opens /auth/gainsight/start in a popup', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'expired' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    screen.getByText('Reconnect Gainsight').click();
    expect(windowOpenSpy).toHaveBeenCalledWith(
      '/auth/gainsight/start', '_blank', 'width=500,height=600,popup=yes'
    );
  });

  it('window.open features string forces popup-window behavior (regression guard)', () => {
    // G.2 Phase 7 verification surfaced that bare window.open(url, '_blank')
    // silently regressed to same-tab navigation in modern Chrome. The
    // features string (width + height + popup=yes) is required for the
    // OAuth callback to land in a real popup window so its postMessage
    // dispatch finds window.opener and the parent banner refreshes
    // automatically. Pinned here so a future "simplify" pass doesn't
    // strip the features string and silently regress the UX again.
    setHookValue({ gmail: 'expired', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    screen.getByText('Reconnect Google').click();
    const args = windowOpenSpy.mock.calls[0];
    expect(args[0]).toBe('/auth/google/start');
    expect(args[1]).toBe('_blank');
    expect(args[2]).toMatch(/width=\d+/);
    expect(args[2]).toMatch(/height=\d+/);
    expect(args[2]).toContain('popup=yes');
  });

  it('preserves "Open settings" as a secondary action (with new copy)', () => {
    setHookValue({ gmail: 'expired', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    const onOpenSettings = vi.fn();
    render(<ConnectionExpiryBanner onOpenSettings={onOpenSettings} />);
    const link = screen.getByText('open settings to manage all connections');
    expect(link).toBeInTheDocument();
    link.click();
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  it('does NOT render reconnect buttons when no providers expired', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.queryByText(/^Reconnect /)).toBeNull();
  });

  it('only renders one Google reconnect button when both gmail and calendar expired', () => {
    setHookValue({ gmail: 'expired', calendar: 'expired', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    const buttons = screen.getAllByText('Reconnect Google');
    expect(buttons.length).toBe(1);
    expect(screen.queryByText('Reconnect Gmail')).toBeNull();
    expect(screen.queryByText('Reconnect Calendar')).toBeNull();
  });
});

// ── 5. Phase 5: dismiss is state-keyed, not sessionStorage-permanent ────────

describe('Banner dismiss-on-state-change (Phase 5)', () => {
  beforeEach(() => {
    useConnectionStatus.mockReset();
    try { sessionStorage.clear(); } catch { /* ignore */ }
  });
  afterEach(() => { cleanup(); });

  it('dismissing the banner suppresses it while the expired set stays the same', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    const { rerender } = render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.getByRole('alert')).toBeInTheDocument();

    // Click dismiss.
    screen.getByLabelText('Dismiss').click();

    // Rerender with the SAME expired set — banner should stay hidden.
    rerender(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('banner reappears when a new provider becomes expired mid-session (set {} -> {google})', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    const { rerender } = render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.queryByRole('alert')).toBeNull();

    // Google now expires.
    setHookValue({ gmail: 'expired', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    rerender(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/Your Google connection has expired\./)).toBeInTheDocument();
  });

  it('banner reappears when the expired set changes via reconnect (set {google,hubspot} -> {hubspot}, dismiss cleared)', () => {
    // Start with two expired.
    setHookValue({ gmail: 'expired', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    const { rerender } = render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.getByText(/Your Google and HubSpot connections have expired\./)).toBeInTheDocument();

    // User dismisses while two are expired.
    screen.getByLabelText('Dismiss').click();
    rerender(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.queryByRole('alert')).toBeNull();

    // Google reconnects; HubSpot still expired. Set changed -> dismiss clears.
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    rerender(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/Your HubSpot connection has expired\./)).toBeInTheDocument();
  });

  it('banner stays dismissed when set is unchanged (dismiss persists for that exact set)', () => {
    setHookValue({ gmail: 'expired', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    const { rerender } = render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    screen.getByLabelText('Dismiss').click();

    // Multiple re-renders with the SAME hook state — banner stays hidden.
    for (let i = 0; i < 3; i++) {
      rerender(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
      expect(screen.queryByRole('alert')).toBeNull();
    }
  });

  it('no longer writes to sessionStorage on dismiss (legacy behavior removed)', () => {
    setHookValue({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);
    expect(sessionStorage.getItem('cs_agent_expiry_banner_dismissed')).toBeNull();

    screen.getByLabelText('Dismiss').click();
    // Phase 5 dropped the sessionStorage write — dismiss is in-memory only.
    expect(sessionStorage.getItem('cs_agent_expiry_banner_dismissed')).toBeNull();
  });
});

// ── 4b. Phase 4 integration: popup oauth_complete refreshes hook ─────────────
//
// This is the end-to-end check that the reconnect-popup → postMessage
// → hook-refetch flow lands. Real useConnectionStatus (no mock) +
// mocked api.get. Simulates the popup's success callback by
// dispatching the same message shape that main.py's OAuth callback
// HTML emits.

describe('Phase 4 integration: hook refetches after popup oauth_complete', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doUnmock('../src/lib/useConnectionStatus');
  });
  afterEach(() => { cleanup(); vi.clearAllMocks(); vi.useRealTimers(); });

  it('after window dispatches oauth_complete, hook refetches /app/connection-status', async () => {
    vi.doMock('../src/lib/api', () => ({
      default: { get: vi.fn(), post: vi.fn() },
    }));
    const apiModule = await import('../src/lib/api');
    const hookModule = await import('../src/lib/useConnectionStatus');
    const bannerModule = await import('../src/components/connections/ConnectionExpiryBanner');

    apiModule.default.get.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        gmail: 'expired', calendar: 'ok', hubspot: 'ok', gainsight: 'missing',
      }),
    });
    hookModule.__resetForTests();
    const Banner = bannerModule.default;

    vi.useFakeTimers({ shouldAdvanceTime: true });

    render(<Banner onOpenSettings={() => {}} />);

    // First fetch lands on mount.
    await waitFor(() => expect(apiModule.default.get).toHaveBeenCalledTimes(1));

    // Advance past the dedup window, then simulate the popup closing
    // and dispatching oauth_complete from the OAuth callback HTML.
    vi.advanceTimersByTime(1500);
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'oauth_complete', provider: 'google' },
      origin: window.location.origin,
    }));

    await waitFor(() => expect(apiModule.default.get).toHaveBeenCalledTimes(2));
  });
});
