/**
 * Settings v1 (A) — ConnectionsSection + getGoogleGroupState.
 *
 * Pins:
 *   - dots reflect the hook's per-provider states (via data-state)
 *   - Reconnect shows ONLY for expired/missing rows; absent when ok
 *   - Google Reconnect opens /auth/google/start with NO scope=write
 *   - HubSpot / Gainsight Reconnect open their own routes
 *   - both-CRM-missing renders the muted "No CRM connected yet." line
 *   - a fully healthy section renders no action links
 *   - getGoogleGroupState severity worst-of ordering
 *
 * useConnectionStatus is mocked so we drive providers directly. window.open
 * is spied so we read the exact reconnect URL (openProviderPopup runs for
 * real — that integration is the point of the URL assertions).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

vi.mock('../src/lib/useConnectionStatus', () => ({
  default: vi.fn(),
}));

import useConnectionStatus from '../src/lib/useConnectionStatus';
import ConnectionsSection from '../src/components/settings/ConnectionsSection';
import { getGoogleGroupState } from '../src/lib/connectionExpiry';

function setProviders(providers) {
  useConnectionStatus.mockReturnValue({ providers, isLoading: false, error: null, refetch: vi.fn() });
}

let openSpy;
beforeEach(() => {
  useConnectionStatus.mockReset();
  openSpy = vi.spyOn(window, 'open').mockImplementation(() => ({ closed: false }));
});
afterEach(() => {
  cleanup();
  openSpy.mockRestore();
});

describe('ConnectionsSection — rendering', () => {
  it('renders one dot per row reflecting hook states', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing', slack: 'ok', zoom: 'ok' });
    const { container } = render(<ConnectionsSection />);

    const dots = container.querySelectorAll('[data-state]');
    // Google group (worst-of ok/ok = ok) + HubSpot (expired) + Slack (ok) +
    // Zoom (ok), both always rendered in the Communications group. Gainsight
    // is missing so it gets no row.
    const states = Array.from(dots).map((d) => d.getAttribute('data-state'));
    expect(states).toEqual(['ok', 'expired', 'ok', 'ok']);
  });

  it('healthy section renders no Reconnect links', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing', slack: 'ok', zoom: 'ok' });
    render(<ConnectionsSection />);

    expect(screen.queryByRole('button', { name: 'Reconnect' })).toBeNull();
    // Connected status text is shown instead.
    expect(screen.getAllByText('Connected').length).toBeGreaterThan(0);
  });

  it('Google Reconnect shows when degraded and opens /auth/google/start with NO scope=write', () => {
    setProviders({ gmail: 'expired', calendar: 'ok', hubspot: 'missing', gainsight: 'missing', slack: 'ok', zoom: 'ok' });
    render(<ConnectionsSection />);

    const btn = screen.getByRole('button', { name: 'Reconnect' });
    fireEvent.click(btn);

    expect(openSpy).toHaveBeenCalledTimes(1);
    const url = openSpy.mock.calls[0][0];
    expect(url).toBe('/auth/google/start');
    expect(url).not.toContain('scope=write');
    // popup features string preserved
    expect(openSpy.mock.calls[0][2]).toBe('width=500,height=600,popup=yes');
  });

  it('Google row shows no Reconnect when healthy', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing', slack: 'ok', zoom: 'ok' });
    render(<ConnectionsSection />);
    // The only row with an action would be a degraded one; none here.
    expect(screen.queryByRole('button', { name: 'Reconnect' })).toBeNull();
  });

  it('HubSpot Reconnect opens /auth/hubspot/start', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing', slack: 'ok', zoom: 'ok' });
    render(<ConnectionsSection />);

    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
    expect(openSpy.mock.calls[0][0]).toBe('/auth/hubspot/start');
  });

  it('Gainsight Reconnect opens /auth/gainsight/start', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'missing', gainsight: 'expired', slack: 'ok', zoom: 'ok' });
    render(<ConnectionsSection />);

    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
    expect(openSpy.mock.calls[0][0]).toBe('/auth/gainsight/start');
  });

  it('renders the Slack Communications row and Reconnect opens /auth/slack/start', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'ok', slack: 'missing', zoom: 'ok' });
    render(<ConnectionsSection />);

    expect(screen.getByText('Communications')).toBeInTheDocument();
    expect(screen.getByText('Slack')).toBeInTheDocument();
    // Slack is the only degraded row, so the single Reconnect is its own.
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
    expect(openSpy.mock.calls[0][0]).toBe('/auth/slack/start');
  });

  it('renders the Zoom Communications row and Reconnect opens /auth/zoom/start', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'ok', slack: 'ok', zoom: 'missing' });
    render(<ConnectionsSection />);

    expect(screen.getByText('Communications')).toBeInTheDocument();
    expect(screen.getByText('Zoom')).toBeInTheDocument();
    // Zoom is the only degraded row, so the single Reconnect is its own.
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
    expect(openSpy.mock.calls[0][0]).toBe('/auth/zoom/start');
  });

  it('renders both CRM rows when both connected', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'ok' });
    render(<ConnectionsSection />);
    expect(screen.getByText('HubSpot')).toBeInTheDocument();
    expect(screen.getByText('Gainsight')).toBeInTheDocument();
  });

  it('renders muted line when both CRMs missing', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'missing', gainsight: 'missing' });
    render(<ConnectionsSection />);

    expect(screen.getByText('No CRM connected yet.')).toBeInTheDocument();
    expect(screen.queryByText('HubSpot')).toBeNull();
    expect(screen.queryByText('Gainsight')).toBeNull();
  });

  it('always shows the Google group with Gmail/Calendar/Drive sub-copy', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'missing', gainsight: 'missing' });
    render(<ConnectionsSection />);
    expect(screen.getByText('Google')).toBeInTheDocument();
    expect(screen.getByText('Gmail, Calendar, and Drive')).toBeInTheDocument();
  });

  it('shows Loading while providers is null', () => {
    setProviders(null);
    render(<ConnectionsSection />);
    expect(screen.getByText('Loading…')).toBeInTheDocument();
  });
});

describe('getGoogleGroupState — worst-of severity', () => {
  it('expired wins over everything', () => {
    expect(getGoogleGroupState({ gmail: 'expired', calendar: 'ok' })).toBe('expired');
    expect(getGoogleGroupState({ gmail: 'ok', calendar: 'expired' })).toBe('expired');
    expect(getGoogleGroupState({ gmail: 'expired', calendar: 'missing' })).toBe('expired');
  });

  it('missing wins over error and ok', () => {
    expect(getGoogleGroupState({ gmail: 'missing', calendar: 'ok' })).toBe('missing');
    expect(getGoogleGroupState({ gmail: 'error', calendar: 'missing' })).toBe('missing');
  });

  it('error wins over ok', () => {
    expect(getGoogleGroupState({ gmail: 'error', calendar: 'ok' })).toBe('error');
  });

  it('ok only when both ok', () => {
    expect(getGoogleGroupState({ gmail: 'ok', calendar: 'ok' })).toBe('ok');
  });

  it('returns error when providers is null', () => {
    expect(getGoogleGroupState(null)).toBe('error');
  });
});
