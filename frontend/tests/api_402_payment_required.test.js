/**
 * G.1 Stage 2 — api.js 402 contract.
 *
 * Pins the documented response handling for ApiSubscriptionGatedError
 * and the window CustomEvent that wakes AppLayout's existing
 * checkSubscription() path.
 *
 * The event-dispatch test is load-bearing — it's the integration
 * contract between api.js and AppLayout. Without that, the flat 402
 * schema parse is correct but the modal never flips on active-action
 * 402s; the only channel left is the focus-triggered re-check, which
 * misses 402s landing mid-tab.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import api, { ApiSubscriptionGatedError } from '../src/lib/api';

describe('api.js 402 contract', () => {
  let originalFetch;
  let originalDispatchEvent;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    originalDispatchEvent = window.dispatchEvent;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.dispatchEvent = originalDispatchEvent;
  });

  it('throws ApiSubscriptionGatedError on 402 with status and requestId populated', async () => {
    globalThis.fetch = vi.fn(async () => new Response(
      JSON.stringify({ error: 'subscription_required', status: 'canceled' }),
      { status: 402, headers: { 'Content-Type': 'application/json' } },
    ));

    let caught;
    try {
      await api.get('/app/connection-status');
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiSubscriptionGatedError);
    expect(caught.message).toBe('subscription_required');
    expect(caught.status).toBe('canceled');
    expect(caught.requestId).toBeTypeOf('string');
    expect(caught.requestId.length).toBeGreaterThan(8);
  });

  it('handles malformed 402 body without raising during parse; status defaults to "unknown"', async () => {
    globalThis.fetch = vi.fn(async () => new Response(
      'not json {{{',
      { status: 402, headers: { 'Content-Type': 'application/json' } },
    ));

    let caught;
    try {
      await api.get('/app/connection-status');
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiSubscriptionGatedError);
    expect(caught.message).toBe('subscription_required');
    expect(caught.status).toBe('unknown');
    expect(caught.requestId).toBeTypeOf('string');
  });

  it('dispatches subscription:gated-detected CustomEvent before throwing', async () => {
    globalThis.fetch = vi.fn(async () => new Response(
      JSON.stringify({ error: 'subscription_required', status: 'past_due' }),
      { status: 402, headers: { 'Content-Type': 'application/json' } },
    ));

    const received = [];
    function listener(ev) {
      received.push({
        type: ev.type,
        detail: ev.detail,
        // Snapshot whether the error has been thrown yet by recording
        // the order: the event must fire before the throw.
      });
    }
    window.addEventListener('subscription:gated-detected', listener);

    let caught;
    try {
      await api.get('/app/connection-status');
    } catch (e) {
      caught = e;
    }
    window.removeEventListener('subscription:gated-detected', listener);

    // Event fired exactly once with the documented detail shape.
    expect(received).toHaveLength(1);
    expect(received[0].type).toBe('subscription:gated-detected');
    expect(received[0].detail.status).toBe('past_due');
    expect(received[0].detail.requestId).toBeTypeOf('string');

    // Throw happened after the dispatch — the listener saw the event
    // before the caller observed the rejection.
    expect(caught).toBeInstanceOf(ApiSubscriptionGatedError);
    expect(caught.requestId).toBe(received[0].detail.requestId);
  });
});
