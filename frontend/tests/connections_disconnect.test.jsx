/**
 * Settings v1 (B) — ConnectionsSection Disconnect affordance.
 *
 * Pins:
 *   - Disconnect shows for an "ok" CRM and an "expired" CRM
 *   - Disconnect is absent for a "missing" CRM (row not rendered at all)
 *   - the in-section confirm copy is REVERSIBLE wording (no "permanent" /
 *     "cannot be undone")
 *   - confirm POSTs /app/settings/disconnect-crm with the right provider and
 *     confirm:true
 *   - refetch() (the hook's) is invoked on a successful disconnect — disconnect
 *     emits no oauth_complete postMessage, so the dots only heal via refetch
 *   - the error path (non-2xx) leaves the row unchanged and shows the inline
 *     "Could not disconnect" message
 *
 * useConnectionStatus is mocked so we drive providers + spy refetch directly.
 * lib/api is mocked so we control the POST result.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../src/lib/useConnectionStatus', () => ({
  default: vi.fn(),
}));
vi.mock('../src/lib/api', () => ({
  default: { post: vi.fn(), get: vi.fn() },
}));

import useConnectionStatus from '../src/lib/useConnectionStatus';
import api from '../src/lib/api';
import ConnectionsSection from '../src/components/settings/ConnectionsSection';

let refetchSpy;

function setProviders(providers) {
  refetchSpy = vi.fn();
  useConnectionStatus.mockReturnValue({
    providers, isLoading: false, error: null, refetch: refetchSpy,
  });
}

beforeEach(() => {
  useConnectionStatus.mockReset();
  api.post.mockReset();
  // Default: a healthy popup spy so Reconnect clicks don't blow up.
  vi.spyOn(window, 'open').mockImplementation(() => ({ closed: false }));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('ConnectionsSection — Disconnect affordance visibility', () => {
  it('shows Disconnect for an "ok" CRM', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionsSection />);
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
  });

  it('shows Disconnect for an "expired" CRM', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing' });
    render(<ConnectionsSection />);
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
  });

  it('does NOT show Disconnect for a "missing" CRM (row absent)', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'missing', gainsight: 'missing' });
    render(<ConnectionsSection />);
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull();
    // The CRM row itself isn't rendered.
    expect(screen.queryByText('HubSpot')).toBeNull();
  });
});

describe('ConnectionsSection — confirm copy', () => {
  it('uses reversible wording (no "permanent"/"cannot be undone")', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionsSection />);

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));

    expect(
      screen.getByText(/stop using your HubSpot data\. You can reconnect anytime/i),
    ).toBeInTheDocument();
    // Explicitly NOT the destructive/irreversible register.
    expect(screen.queryByText(/permanent/i)).toBeNull();
    expect(screen.queryByText(/cannot be undone/i)).toBeNull();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });
});

describe('ConnectionsSection — disconnect flow', () => {
  it('POSTs the right provider + confirm:true and refetches on success', async () => {
    api.post.mockResolvedValue({ ok: true });
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'missing', gainsight: 'ok' });
    render(<ConnectionsSection />);

    // Open the confirm step, then confirm.
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    // Two "Disconnect" buttons now? No — the row affordance is replaced by the
    // confirm box; the confirm box's primary is also "Disconnect".
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith(
      '/app/settings/disconnect-crm',
      { provider: 'gainsight', confirm: true },
    );
    await waitFor(() => expect(refetchSpy).toHaveBeenCalledTimes(1));
  });

  it('error path leaves the row unchanged and shows inline error', async () => {
    api.post.mockResolvedValue({ ok: false, status: 500 });
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionsSection />);

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));

    expect(await screen.findByText(/Could not disconnect, please try again/i))
      .toBeInTheDocument();
    // Row unchanged: refetch NOT called, the HubSpot row + its dot still there.
    expect(refetchSpy).not.toHaveBeenCalled();
    expect(screen.getByText('HubSpot')).toBeInTheDocument();
    // Confirm step stayed open (Cancel still present) so the user can retry.
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('Cancel closes the confirm step without POSTing', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionsSection />);

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
    expect(api.post).not.toHaveBeenCalled();
    // The plain Disconnect affordance is back.
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
  });
});
