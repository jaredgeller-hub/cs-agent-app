/**
 * Tests for the subscription gate decision in AppLayout.
 *
 * Covers the regression introduced when /app/subscription-status returns
 * 401 (Clerk session not yet valid, transient token-refresh window): the
 * gate must NOT render. Comp accounts and any user mid-refresh should
 * never see the trial-ended modal because of an auth blip.
 *
 * Also pins the happy paths:
 *   - 200 with is_gated=false: no modal
 *   - 200 with is_gated=true: modal renders
 *
 * AppLayout pulls a stack of hooks (useChat, useAccounts, useTimeline,
 * useDigest, useAlerts, Clerk useUser/useClerk/UserButton, useAuthReady).
 * We mock all of them so the test stays focused on the gate decision.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';

vi.mock('@clerk/clerk-react', () => {
  const UserButton = ({ children }) => <div data-testid="user-button">{children}</div>;
  UserButton.MenuItems = ({ children }) => <>{children}</>;
  UserButton.Action = () => null;
  return {
    useUser: () => ({ user: { firstName: 'Test', emailAddresses: [{ emailAddress: 'test@example.com' }] } }),
    useClerk: () => ({ signOut: vi.fn() }),
    UserButton,
  };
});

vi.mock('../src/components/auth/AuthTokenProvider', () => ({
  useAuthReady: () => true,
  default: ({ children }) => <>{children}</>,
}));

vi.mock('../src/hooks/useChat', () => ({
  default: () => ({ messages: [], loading: false, sendMessage: vi.fn(), appendMessage: vi.fn() }),
}));
vi.mock('../src/hooks/useDigest', () => ({ default: () => {} }));
vi.mock('../src/hooks/useAlerts', () => ({ default: () => {} }));
vi.mock('../src/hooks/useAccounts', () => ({ default: () => ({ accounts: [] }) }));
vi.mock('../src/hooks/useTimeline', () => ({ default: () => ({ events: [], refresh: vi.fn() }) }));

vi.mock('../src/components/chat/ChatWindow', () => ({ default: () => <div data-testid="chat-window" /> }));
vi.mock('../src/components/chat/ChatInput', () => ({ default: () => <div data-testid="chat-input" /> }));
vi.mock('../src/components/layout/Sidebar', () => ({ default: () => <div data-testid="sidebar" /> }));
vi.mock('../src/components/layout/Timeline', () => ({ default: () => <div data-testid="timeline" /> }));
vi.mock('../src/components/connections/ConnectionStatus', () => ({ default: () => null }));
vi.mock('../src/components/settings/SettingsModal', () => ({ default: () => null }));
vi.mock('../src/components/billing/ManageSubscriptionMenuItem', () => ({
  openBillingPortal: vi.fn(),
  isManageSubscriptionVisible: () => false,
}));

vi.mock('../src/lib/api', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

import api from '../src/lib/api';
import AppLayout from '../src/components/layout/AppLayout';

const GATE_HEADING = /Your trial has ended/i;

describe('AppLayout subscription gate decision', () => {
  beforeEach(() => {
    api.get.mockReset();
    api.post.mockReset();
    // AppLayout fires a profile-sync POST on mount; give it a default
    // resolved promise so the .catch() chain doesn't blow up.
    api.post.mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('does not render the gate when subscription-status returns 401', async () => {
    const err = new Error('Unauthorized');
    err.requestId = 'rid-test-401';
    api.get.mockRejectedValue(err);

    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    render(<AppLayout />);

    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/app/subscription-status'));
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.queryByText(GATE_HEADING)).not.toBeInTheDocument();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it('does not render the gate when subscription-status returns 200 with is_gated=false', async () => {
    api.get.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        status: 'active',
        is_gated: false,
        trial_ends_at: null,
        has_stripe_customer: true,
      }),
    });

    render(<AppLayout />);

    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/app/subscription-status'));
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.queryByText(GATE_HEADING)).not.toBeInTheDocument();
  });

  it('renders the gate when subscription-status returns 200 with is_gated=true', async () => {
    api.get.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        status: 'canceled',
        is_gated: true,
        trial_ends_at: null,
        has_stripe_customer: true,
      }),
    });

    render(<AppLayout />);

    await waitFor(() => expect(screen.getByText(GATE_HEADING)).toBeInTheDocument());
  });

  it('does not render the gate on a network error (fail open)', async () => {
    const netErr = new Error('Network error');
    netErr.name = 'ApiNetworkError';
    api.get.mockRejectedValue(netErr);

    render(<AppLayout />);

    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/app/subscription-status'));
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.queryByText(GATE_HEADING)).not.toBeInTheDocument();
  });
});
