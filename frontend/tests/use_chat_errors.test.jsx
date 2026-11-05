/**
 * Regression guard for useChat's differentiated error branches.
 *
 * Before: every failure (timeout, 401, parse error, network error)
 * collapsed into one opaque "Something went wrong" message. Reviewers
 * could not tell whether to retry, refresh, or report a bug.
 *
 * After: five branches map each failure mode to a distinct user-facing
 * string. Three of them include a request_id so users can include it
 * in bug reports. This test asserts the exact strings so they cannot
 * silently drift back to a generic fallback.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

import useChat from '../src/hooks/useChat';
import api, { ApiTimeoutError, ApiNetworkError } from '../src/lib/api';

function lastAssistantMessage(result) {
  const msgs = result.current.messages.filter(m => m.role === 'assistant');
  return msgs[msgs.length - 1];
}

describe('useChat error branches', () => {
  let originalConsoleError;

  beforeEach(() => {
    // Silence console.error noise from the hook's debug logging while
    // still letting us assert it was called.
    originalConsoleError = console.error;
    console.error = vi.fn();
  });

  afterEach(() => {
    console.error = originalConsoleError;
    vi.restoreAllMocks();
  });

  it('renders the timeout message with request_id when ApiTimeoutError is thrown', async () => {
    vi.spyOn(api, 'post').mockImplementation(async () => {
      throw new ApiTimeoutError('rid-timeout-abc', 45000);
    });
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage('hello');
    });
    const msg = lastAssistantMessage(result);
    expect(msg.content).toBe(
      'This is taking longer than expected. Please retry. (request id: rid-timeout-abc)'
    );
  });

  it('renders the session-expired message on 401 (no request_id surfaced)', async () => {
    vi.spyOn(api, 'post').mockImplementation(async () => {
      const e = new Error('Unauthorized');
      e.requestId = 'rid-401-xyz';
      throw e;
    });
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage('hello');
    });
    const msg = lastAssistantMessage(result);
    // Per spec: 401 message intentionally does NOT include request_id;
    // the user's action is "refresh the page", not "report this id".
    expect(msg.content).toBe('Your session has expired. Please refresh the page.');
  });

  it('renders the parse-error message with request_id when res.json rejects', async () => {
    // api.post resolves successfully; res.json throws SyntaxError. The
    // hook captures res.requestId before res.json so the parse-error
    // branch can still surface it.
    vi.spyOn(api, 'post').mockImplementation(async () => {
      const fakeRes = {
        requestId: 'rid-parse-err',
        headers: { get: () => null },
        json: () => Promise.reject(new SyntaxError('Unexpected token < in JSON at position 0')),
      };
      return fakeRes;
    });
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage('hello');
    });
    const msg = lastAssistantMessage(result);
    expect(msg.content).toBe(
      "We could not read the server's response. Please retry. (request id: rid-parse-err)"
    );
  });

  it('renders the network-error message with request_id when ApiNetworkError is thrown', async () => {
    vi.spyOn(api, 'post').mockImplementation(async () => {
      throw new ApiNetworkError('rid-net-err', new TypeError('Failed to fetch'));
    });
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage('hello');
    });
    const msg = lastAssistantMessage(result);
    expect(msg.content).toBe(
      'Network error. Please check your connection and retry. (request id: rid-net-err)'
    );
  });

  it('renders the generic fallback for any other error', async () => {
    vi.spyOn(api, 'post').mockImplementation(async () => {
      const e = new Error('boom');
      // No requestId attached; the fallback branch handles that case.
      throw e;
    });
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage('hello');
    });
    const msg = lastAssistantMessage(result);
    // No request id available, so the suffix is omitted.
    expect(msg.content).toBe('Something unexpected happened. Please retry.');
  });

  it('logs structured error context to console.error on every failure', async () => {
    vi.spyOn(api, 'post').mockImplementation(async () => {
      throw new ApiTimeoutError('rid-log-test', 1000);
    });
    const { result } = renderHook(() => useChat());
    await act(async () => {
      await result.current.sendMessage('hello');
    });
    expect(console.error).toHaveBeenCalled();
    const args = console.error.mock.calls[0];
    // First arg is the prefix; second arg is the structured payload.
    expect(args[0]).toBe('[chat] request failed');
    expect(args[1]).toMatchObject({
      requestId: 'rid-log-test',
      errorType: 'timeout',
      errorName: 'ApiTimeoutError',
    });
  });

  it('does not collapse all failures into the same fallback message', async () => {
    // Sanity: timeout vs network vs unknown produce different strings.
    const captured = [];

    vi.spyOn(api, 'post').mockImplementationOnce(async () => {
      throw new ApiTimeoutError('rid-1', 100);
    });
    {
      const { result } = renderHook(() => useChat());
      await act(async () => { await result.current.sendMessage('a'); });
      captured.push(lastAssistantMessage(result).content);
    }

    vi.spyOn(api, 'post').mockImplementationOnce(async () => {
      throw new ApiNetworkError('rid-2', new TypeError('x'));
    });
    {
      const { result } = renderHook(() => useChat());
      await act(async () => { await result.current.sendMessage('b'); });
      captured.push(lastAssistantMessage(result).content);
    }

    vi.spyOn(api, 'post').mockImplementationOnce(async () => {
      throw new Error('weird');
    });
    {
      const { result } = renderHook(() => useChat());
      await act(async () => { await result.current.sendMessage('c'); });
      captured.push(lastAssistantMessage(result).content);
    }

    expect(new Set(captured).size).toBe(3);
  });
});
