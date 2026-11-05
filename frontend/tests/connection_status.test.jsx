/**
 * Tests for ConnectionStatus and ConnectionExpiryBanner.
 *
 * Pins per-state rendering for the four token states (ok, expired,
 * missing, error) and confirms the reconnect link targets match the
 * /auth/<provider>/start backend routes verified in pre-flight.
 *
 * Mocks the api module so no network traffic happens.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';

vi.mock('../src/lib/api', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

import api from '../src/lib/api';
import { __resetForTests } from '../src/lib/useConnectionStatus';
import ConnectionStatus from '../src/components/connections/ConnectionStatus';
import ConnectionExpiryBanner from '../src/components/connections/ConnectionExpiryBanner';

function mockResponse(body) {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  };
}

describe('ConnectionStatus per-state rendering', () => {
  beforeEach(() => {
    __resetForTests();
    api.get.mockReset();
    try { sessionStorage.clear(); } catch { /* ignore */ }
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('renders all-ok with no reconnect links', async () => {
    api.get.mockResolvedValue(mockResponse({
      gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing',
    }));

    render(<ConnectionStatus />);

    await waitFor(() => expect(screen.getByText('Gmail')).toBeInTheDocument());
    expect(screen.getByText('Calendar')).toBeInTheDocument();
    expect(screen.getByText('HubSpot')).toBeInTheDocument();
    expect(screen.queryByText('Reconnect')).not.toBeInTheDocument();
  });

  it('Reconnect opens Google popup (not same-tab nav) when expired (gmail)', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    api.get.mockResolvedValue(mockResponse({
      gmail: 'expired', calendar: 'expired', hubspot: 'ok', gainsight: 'missing',
    }));

    render(<ConnectionStatus />);

    await waitFor(() => expect(screen.getAllByText('Reconnect')).toHaveLength(2));
    const reconnects = screen.getAllByText('Reconnect');
    // Reconnect must be a button (popup), not an anchor (same-tab nav) — F-4.
    reconnects.forEach((el) => expect(el.tagName).toBe('BUTTON'));
    // Both gmail and calendar heal via one /auth/google/start popup.
    fireEvent.click(reconnects[0]);
    expect(openSpy).toHaveBeenCalledWith(
      '/auth/google/start', '_blank', 'width=500,height=600,popup=yes',
    );
    openSpy.mockRestore();
  });

  it('Connect opens Gainsight popup for missing CRM (gainsight) over hubspot', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    // Gainsight present takes priority over hubspot when picking which CRM dot to show
    api.get.mockResolvedValue(mockResponse({
      gmail: 'ok', calendar: 'ok', hubspot: 'missing', gainsight: 'expired',
    }));

    render(<ConnectionStatus />);

    await waitFor(() => expect(screen.getByText('Gainsight')).toBeInTheDocument());
    const reconnect = screen.getByText('Reconnect');
    expect(reconnect.tagName).toBe('BUTTON');
    fireEvent.click(reconnect);
    expect(openSpy).toHaveBeenCalledWith(
      '/auth/gainsight/start', '_blank', 'width=500,height=600,popup=yes',
    );
    expect(screen.queryByText('HubSpot')).not.toBeInTheDocument();
    openSpy.mockRestore();
  });

  it('falls back to HubSpot popup when gainsight is missing', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    api.get.mockResolvedValue(mockResponse({
      gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing',
    }));

    render(<ConnectionStatus />);

    await waitFor(() => expect(screen.getByText('HubSpot')).toBeInTheDocument());
    const reconnect = screen.getByText('Reconnect');
    expect(reconnect.tagName).toBe('BUTTON');
    fireEvent.click(reconnect);
    expect(openSpy).toHaveBeenCalledWith(
      '/auth/hubspot/start', '_blank', 'width=500,height=600,popup=yes',
    );
    openSpy.mockRestore();
  });

  it('renders nothing when api.get fails (preserves last state)', async () => {
    api.get.mockRejectedValue(new Error('network'));

    const { container } = render(<ConnectionStatus />);

    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });
});

describe('ConnectionExpiryBanner', () => {
  beforeEach(() => {
    __resetForTests();
    api.get.mockReset();
    try { sessionStorage.clear(); } catch { /* ignore */ }
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('does not render when no providers are expired', async () => {
    api.get.mockResolvedValue(mockResponse({
      gmail: 'ok', calendar: 'ok', hubspot: 'missing', gainsight: 'missing',
    }));

    const { container } = render(<ConnectionExpiryBanner />);

    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it('renders banner when at least one provider is expired', async () => {
    api.get.mockResolvedValue(mockResponse({
      gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing',
    }));

    render(<ConnectionExpiryBanner onOpenSettings={() => {}} />);

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    // Phase 3: per-provider naming replaces "one or more" copy.
    expect(screen.getByText(/Your HubSpot connection has expired/i)).toBeInTheDocument();
    // Phase 4: direct per-provider reconnect button + secondary settings link.
    expect(screen.getByText('Reconnect HubSpot')).toBeInTheDocument();
    expect(screen.getByText('open settings to manage all connections')).toBeInTheDocument();
  });

  it('does not render banner for "missing" state', async () => {
    api.get.mockResolvedValue(mockResponse({
      gmail: 'missing', calendar: 'missing', hubspot: 'missing', gainsight: 'missing',
    }));

    const { container } = render(<ConnectionExpiryBanner />);

    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it('does not render banner for "error" state', async () => {
    api.get.mockResolvedValue(mockResponse({
      gmail: 'error', calendar: 'error', hubspot: 'error', gainsight: 'error',
    }));

    const { container } = render(<ConnectionExpiryBanner />);

    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  // Phase 5: dismiss is no longer keyed to sessionStorage — it's
  // state-keyed to the SET of currently expired providers. Stale
  // sessionStorage entries from older sessions are ignored.
  it('does NOT respect legacy sessionStorage dismissal flag (Phase 5 dropped it)', async () => {
    sessionStorage.setItem('cs_agent_expiry_banner_dismissed', '1');
    api.get.mockResolvedValue(mockResponse({
      gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing',
    }));

    render(<ConnectionExpiryBanner />);

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
  });
});
