/**
 * Session B Commit 4 — ConnectionsSection "Connect a CRM" control.
 *
 * Closes the disconnect dead-end: after disconnecting the only CRM the section
 * used to show "No CRM connected yet." with no way back. A single Connect-a-CRM
 * control now appears whenever ANY CRM is missing and expands to offer the
 * missing CRMs, each reusing the existing /auth/{provider}/start OAuth popup.
 *
 * Pins:
 *   - both missing: control renders; "No CRM connected yet." lead line renders
 *     ABOVE it; no CrmRow renders
 *   - expand reveals exactly the missing CRMs; each calls openProviderPopup with
 *     the right key; Cancel collapses
 *   - one connected + one missing: the connected CrmRow (with Disconnect) renders
 *     AND the control offers only the missing CRM; no lead line
 *   - both connected: no control at all
 *   - an "error" CRM is NOT offered in the control (transient, not absent)
 *   - an "expired" CRM renders its CrmRow (Reconnect) and is NOT duplicated in
 *     the control
 *
 * useConnectionStatus is mocked to drive providers. lib/connectionExpiry is
 * module-mocked so openProviderPopup is a spy while getProviderDisplayName /
 * STATE_COLORS / getGoogleGroupState keep their real behavior.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

vi.mock('../src/lib/useConnectionStatus', () => ({
  default: vi.fn(),
}));
vi.mock('../src/lib/connectionExpiry', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, openProviderPopup: vi.fn() };
});

import useConnectionStatus from '../src/lib/useConnectionStatus';
import { openProviderPopup } from '../src/lib/connectionExpiry';
import ConnectionsSection from '../src/components/settings/ConnectionsSection';

function setProviders(providers) {
  useConnectionStatus.mockReturnValue({
    providers, isLoading: false, error: null, refetch: vi.fn(),
  });
}

beforeEach(() => {
  useConnectionStatus.mockReset();
  openProviderPopup.mockReset();
});
afterEach(cleanup);

describe('ConnectionsSection — Connect a CRM control', () => {
  it('both missing: control + lead line (above), no CrmRow', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'missing', gainsight: 'missing' });
    render(<ConnectionsSection />);

    const lead = screen.getByText('No CRM connected yet.');
    const connect = screen.getByRole('button', { name: 'Connect a CRM' });
    expect(lead).toBeInTheDocument();
    expect(connect).toBeInTheDocument();
    // Lead line is positioned before the control in the DOM.
    expect(
      lead.compareDocumentPosition(connect) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // No CrmRow for a missing CRM, and no Disconnect affordance.
    expect(screen.queryByText('HubSpot')).toBeNull();
    expect(screen.queryByText('Gainsight')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull();
  });

  it('expand reveals both missing CRMs; each opens the right popup; Cancel collapses', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'missing', gainsight: 'missing' });
    render(<ConnectionsSection />);

    fireEvent.click(screen.getByRole('button', { name: 'Connect a CRM' }));

    const hubspotBtn = screen.getByRole('button', { name: 'Connect HubSpot' });
    const gainsightBtn = screen.getByRole('button', { name: 'Connect Gainsight' });
    expect(hubspotBtn).toBeInTheDocument();
    expect(gainsightBtn).toBeInTheDocument();
    // Exactly the two missing CRMs offered — collapsed "Connect a CRM" is gone.
    expect(screen.queryByRole('button', { name: 'Connect a CRM' })).toBeNull();

    fireEvent.click(hubspotBtn);
    expect(openProviderPopup).toHaveBeenCalledWith('hubspot');
    fireEvent.click(gainsightBtn);
    expect(openProviderPopup).toHaveBeenCalledWith('gainsight');

    // Cancel collapses back to the single control.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Connect a CRM' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect HubSpot' })).toBeNull();
  });

  it('one connected + one missing: connected row (with Disconnect) + control offers only the missing one', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'missing' });
    render(<ConnectionsSection />);

    // Connected HubSpot row renders with its Disconnect affordance.
    expect(screen.getByText('HubSpot')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
    // No "none connected" lead line when at least one CRM is connected.
    expect(screen.queryByText('No CRM connected yet.')).toBeNull();

    // Control offers only the missing Gainsight.
    fireEvent.click(screen.getByRole('button', { name: 'Connect a CRM' }));
    expect(screen.getByRole('button', { name: 'Connect Gainsight' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect HubSpot' })).toBeNull();
  });

  it('both connected: no Connect control at all', () => {
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'ok', gainsight: 'ok' });
    render(<ConnectionsSection />);

    expect(screen.queryByRole('button', { name: 'Connect a CRM' })).toBeNull();
    expect(screen.queryByText('No CRM connected yet.')).toBeNull();
    // Both rows present.
    expect(screen.getByText('HubSpot')).toBeInTheDocument();
    expect(screen.getByText('Gainsight')).toBeInTheDocument();
  });

  it('an "error" CRM is not offered in the control', () => {
    // hubspot error (transient) → renders a CrmRow, NOT a connect option.
    // gainsight missing → the only connect option.
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'error', gainsight: 'missing' });
    render(<ConnectionsSection />);

    fireEvent.click(screen.getByRole('button', { name: 'Connect a CRM' }));
    expect(screen.getByRole('button', { name: 'Connect Gainsight' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect HubSpot' })).toBeNull();
  });

  it('an "expired" CRM renders its CrmRow (Reconnect) and is not duplicated in the control', () => {
    // slack + zoom ok so the Communications rows add no second Reconnect.
    setProviders({ gmail: 'ok', calendar: 'ok', hubspot: 'expired', gainsight: 'missing', slack: 'ok', zoom: 'ok' });
    render(<ConnectionsSection />);

    // Expired HubSpot still renders a CrmRow with Reconnect.
    expect(screen.getByText('HubSpot')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument();

    // Connect control offers only the missing Gainsight — no duplicate HubSpot.
    fireEvent.click(screen.getByRole('button', { name: 'Connect a CRM' }));
    expect(screen.getByRole('button', { name: 'Connect Gainsight' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect HubSpot' })).toBeNull();
  });
});
