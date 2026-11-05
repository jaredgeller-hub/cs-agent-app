/**
 * Tests for the "Manage subscription" user-menu entry point (Day 4 P0 Bug 3).
 *
 * Clerk's <UserButton.MenuItems /> does shallow child-type matching and
 * silently filters out custom component wrappers around <UserButton.Action />.
 * That constraint rules out testing via a rendered component; instead we
 * test the two extracted pieces — the visibility predicate and the click
 * handler — which carry all the non-JSX logic. The JSX itself is a
 * 3-line inline in AppLayout.jsx that any regression would surface
 * immediately in a real Clerk session.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  openBillingPortal,
  isManageSubscriptionVisible,
} from '../src/components/billing/ManageSubscriptionMenuItem';

// Stub the api module so openBillingPortal doesn't hit the network
vi.mock('../src/lib/api', () => ({
  default: {
    get:  vi.fn(),
    post: vi.fn(),
  },
}));

// Import the mocked api after the mock declaration so we can manipulate it
import api from '../src/lib/api';

describe('isManageSubscriptionVisible', () => {
  it('returns true when hasStripeCustomer is true', () => {
    expect(isManageSubscriptionVisible(true)).toBe(true);
  });

  it('returns false when hasStripeCustomer is false', () => {
    expect(isManageSubscriptionVisible(false)).toBe(false);
  });

  it('returns false on null/undefined/truthy-non-true (strict check)', () => {
    expect(isManageSubscriptionVisible(null)).toBe(false);
    expect(isManageSubscriptionVisible(undefined)).toBe(false);
    expect(isManageSubscriptionVisible('yes')).toBe(false);  // string truthy ≠ true
    expect(isManageSubscriptionVisible(1)).toBe(false);       // number truthy ≠ true
  });
});

describe('openBillingPortal', () => {
  beforeEach(() => {
    api.post.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('POSTs /app/billing/portal-session and opens the returned URL in new tab', async () => {
    api.post.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ url: 'https://billing.stripe.com/session/abc123' }),
    });
    const opener = vi.fn();

    const result = await openBillingPortal(opener);

    expect(api.post).toHaveBeenCalledWith('/app/billing/portal-session');
    expect(opener).toHaveBeenCalledWith('https://billing.stripe.com/session/abc123');
    expect(result).toEqual({ ok: true });
  });

  it('returns not-ok without opening when Portal endpoint errors', async () => {
    api.post.mockResolvedValue({
      ok: false,
      status: 400,
      json: () => Promise.resolve({ error: 'No Stripe customer found' }),
    });
    const opener = vi.fn();

    const result = await openBillingPortal(opener);

    expect(opener).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    expect(result.reason).toBe('http_400');
  });

  it('returns not-ok when response has no url field', async () => {
    api.post.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),  // missing url
    });
    const opener = vi.fn();

    const result = await openBillingPortal(opener);

    expect(opener).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    expect(result.reason).toBe('no_url');
  });

  it('swallows network exceptions and returns not-ok', async () => {
    api.post.mockRejectedValue(new Error('Network down'));
    const opener = vi.fn();

    const result = await openBillingPortal(opener);

    expect(opener).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    expect(result.reason).toBe('Network down');
  });

  it('defaults to window.open with _blank + noopener when no opener provided', async () => {
    api.post.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ url: 'https://billing.stripe.com/session/xyz' }),
    });
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);

    await openBillingPortal();

    expect(openSpy).toHaveBeenCalledWith(
      'https://billing.stripe.com/session/xyz',
      '_blank',
      'noopener',
    );
  });
});
