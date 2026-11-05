/**
 * Tests for useConnectionStatus — the shared hook that powers the
 * connection-expiry banner and the provider-dots row.
 *
 * The hook is the single source of truth for /app/connection-status
 * fetches across the app. These tests pin the triggers (mount, focus,
 * postMessage), the origin-validation guard against hostile messages,
 * the dedup window, and the cleanup contract on unmount.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

vi.mock('../src/lib/api', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

import api from '../src/lib/api';
import useConnectionStatus, { __resetForTests } from '../src/lib/useConnectionStatus';

function mockResponse(body) {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  };
}

const ALL_OK = { gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' };

describe('useConnectionStatus', () => {
  beforeEach(() => {
    __resetForTests();
    api.get.mockReset();
    api.get.mockResolvedValue(mockResponse(ALL_OK));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('fetches /app/connection-status on mount', async () => {
    const { result } = renderHook(() => useConnectionStatus());

    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/app/connection-status'));
    await waitFor(() => expect(result.current.providers).toEqual(ALL_OK));
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('re-fetches on window focus event (outside dedup window)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });

    const { result } = renderHook(() => useConnectionStatus());

    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.providers).toEqual(ALL_OK));

    // Move past the 1-second dedup window before triggering focus.
    await act(async () => {
      vi.advanceTimersByTime(1500);
      window.dispatchEvent(new Event('focus'));
      // Let microtasks (the awaited fetch) settle.
      await Promise.resolve();
    });

    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  });

  it('re-fetches when oauth_complete postMessage is received from same origin', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });

    renderHook(() => useConnectionStatus());
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));

    await act(async () => {
      vi.advanceTimersByTime(1500);
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'oauth_complete', provider: 'google' },
        origin: window.location.origin,
      }));
      await Promise.resolve();
    });

    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  });

  it('IGNORES postMessage events with mismatched event.origin (CISO regression guard)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });

    renderHook(() => useConnectionStatus());
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));

    await act(async () => {
      vi.advanceTimersByTime(1500);
      // Hostile origin — must not trigger a refetch even though the
      // shape is otherwise valid. Mirrors the regression guards in
      // OnboardingWizard.jsx and SettingsModal.jsx postMessage listeners.
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'oauth_complete', provider: 'google' },
        origin: 'https://evil.example.com',
      }));
      await Promise.resolve();
    });

    // Give a generous window for any spurious fetch to land.
    await new Promise((r) => setTimeout(r, 50));
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('IGNORES postMessage events without type === "oauth_complete"', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });

    renderHook(() => useConnectionStatus());
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));

    await act(async () => {
      vi.advanceTimersByTime(1500);
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'oauth_canceled', provider: 'google' },
        origin: window.location.origin,
      }));
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'subscription:gated', provider: 'stripe' },
        origin: window.location.origin,
      }));
      window.dispatchEvent(new MessageEvent('message', {
        data: 'plain-string-payload',
        origin: window.location.origin,
      }));
      await Promise.resolve();
    });

    await new Promise((r) => setTimeout(r, 50));
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('returns providers as the flat wire dict from /app/connection-status', async () => {
    const wire = { gmail: 'expired', calendar: 'expired', hubspot: 'ok', gainsight: 'missing' };
    api.get.mockResolvedValue(mockResponse(wire));

    const { result } = renderHook(() => useConnectionStatus());

    await waitFor(() => expect(result.current.providers).toEqual(wire));
    expect(result.current.error).toBeNull();
  });

  it('deduplicates: mount + focus + postMessage within 1 second result in ONE fetch', async () => {
    renderHook(() => useConnectionStatus());

    // Fire focus and postMessage immediately after mount, well within
    // the 1-second dedup window. Only the mount fetch should land.
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'oauth_complete', provider: 'google' },
        origin: window.location.origin,
      }));
      await Promise.resolve();
    });

    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
    // Allow any late fetches to land; confirm still 1.
    await new Promise((r) => setTimeout(r, 50));
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('cleans up focus and message listeners on unmount', async () => {
    const addSpy = vi.spyOn(window, 'addEventListener');
    const removeSpy = vi.spyOn(window, 'removeEventListener');

    const { unmount } = renderHook(() => useConnectionStatus());
    await waitFor(() => expect(api.get).toHaveBeenCalled());

    const focusAdded = addSpy.mock.calls.filter((c) => c[0] === 'focus').length;
    const messageAdded = addSpy.mock.calls.filter((c) => c[0] === 'message').length;
    expect(focusAdded).toBeGreaterThanOrEqual(1);
    expect(messageAdded).toBeGreaterThanOrEqual(1);

    unmount();

    const focusRemoved = removeSpy.mock.calls.filter((c) => c[0] === 'focus').length;
    const messageRemoved = removeSpy.mock.calls.filter((c) => c[0] === 'message').length;
    expect(focusRemoved).toBe(focusAdded);
    expect(messageRemoved).toBe(messageAdded);

    addSpy.mockRestore();
    removeSpy.mockRestore();
  });

  it('refetch() bypasses dedup for explicit callers', async () => {
    const { result } = renderHook(() => useConnectionStatus());
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));

    // Immediately after mount — well inside the 1-second dedup window.
    await act(async () => { await result.current.refetch(); });

    expect(api.get).toHaveBeenCalledTimes(2);
  });
});
