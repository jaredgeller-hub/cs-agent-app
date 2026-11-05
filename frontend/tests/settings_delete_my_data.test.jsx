/**
 * Regression guards for Step 3 frontend — the "Data and privacy" section
 * and delete-my-data confirm flow in SettingsModal.
 *
 * Covers:
 *   - "Delete my data" opens the confirm view WITHOUT firing a request
 *   - Cancel from the confirm view returns to main
 *   - The destructive confirm POSTs { confirm: true } and, on a 200
 *     {"status":"deleted"}, signs the user out via Clerk
 *   - A 200 {"status":"data_deleted_clerk_failed"} also signs the user out
 *     (no user-visible branch from "deleted")
 *   - A 500 routes to the delete-error view ("nothing was deleted")
 *
 * Mocking pattern mirrors tests/settings_modal.test.jsx (vi.mock for Clerk
 * + api, makeResponse helper). signOut is a shared spy so the confirm
 * action's sign-out can be asserted.
 */

import { describe, expect, it, beforeEach, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import SettingsModal from '../src/components/settings/SettingsModal';

// Shared signOut spy — declared before vi.mock; vi.mock factories are
// hoisted, so we reference it lazily inside the returned function.
const signOutSpy = vi.fn();
vi.mock('@clerk/clerk-react', () => ({
  useUser: () => ({ user: { id: 'user_test_123' } }),
  useClerk: () => ({ signOut: signOutSpy }),
}));

vi.mock('../src/lib/api', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));
import api from '../src/lib/api';


function makeResponse({ ok, status, body }) {
  return { ok, status, json: () => Promise.resolve(body ?? {}) };
}

function renderOpen(onClose = () => {}) {
  return render(<SettingsModal isOpen={true} onClose={onClose} />);
}

async function waitForLoaded() {
  await waitFor(() => {
    expect(screen.queryByText('Loading…')).toBeNull();
  });
}


beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
  signOutSpy.mockReset();
  // Initial GET for the Google-actions section — disabled, no scope.
  api.get.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
    enabled: false, scope_granted: false,
  } }));
});


describe('SettingsModal — delete-my-data flow', () => {
  it('test_delete_button_opens_confirm_view_without_request', async () => {
    renderOpen();
    await waitForLoaded();

    // Section + button present in main view
    expect(screen.getByText('Data and privacy')).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByText('Delete my data')); });

    // Confirm view shown, NO request fired
    expect(screen.getByText('Permanently delete everything')).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
    // Spell out the consequence, not a generic "are you sure"
    expect(screen.getByText(/will not be able to log back in/i)).toBeInTheDocument();
  });

  it('test_cancel_from_confirm_returns_to_main', async () => {
    renderOpen();
    await waitForLoaded();

    await act(async () => { fireEvent.click(screen.getByText('Delete my data')); });
    expect(screen.getByText('Permanently delete everything')).toBeInTheDocument();

    await act(async () => { fireEvent.click(screen.getByText('Cancel')); });

    // Back to main — the Google toggle is rendered again, confirm gone
    expect(screen.getByRole('checkbox')).toBeInTheDocument();
    expect(screen.queryByText('Permanently delete everything')).toBeNull();
    expect(api.post).not.toHaveBeenCalled();
  });

  it('test_confirm_posts_and_signs_out_on_deleted', async () => {
    api.post.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      status: 'deleted',
    } }));
    renderOpen();
    await waitForLoaded();

    await act(async () => { fireEvent.click(screen.getByText('Delete my data')); });
    await act(async () => {
      fireEvent.click(screen.getByText('Permanently delete everything'));
    });

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/app/settings/delete-my-data', { confirm: true });
    });
    await waitFor(() => {
      expect(signOutSpy).toHaveBeenCalledWith({ redirectUrl: '/' });
    });
  });

  it('test_clerk_failed_status_also_signs_out', async () => {
    api.post.mockResolvedValue(makeResponse({ ok: true, status: 200, body: {
      status: 'data_deleted_clerk_failed',
    } }));
    renderOpen();
    await waitForLoaded();

    await act(async () => { fireEvent.click(screen.getByText('Delete my data')); });
    await act(async () => {
      fireEvent.click(screen.getByText('Permanently delete everything'));
    });

    await waitFor(() => {
      expect(signOutSpy).toHaveBeenCalledWith({ redirectUrl: '/' });
    });
  });

  it('test_500_shows_delete_error_and_does_not_sign_out', async () => {
    api.post.mockResolvedValue(makeResponse({ ok: false, status: 500, body: {} }));
    renderOpen();
    await waitForLoaded();

    await act(async () => { fireEvent.click(screen.getByText('Delete my data')); });
    await act(async () => {
      fireEvent.click(screen.getByText('Permanently delete everything'));
    });

    await waitFor(() => {
      expect(screen.getByText(/nothing was deleted/i)).toBeInTheDocument();
    });
    expect(signOutSpy).not.toHaveBeenCalled();
    // Back-to-settings recovery control present
    expect(screen.getByText('Back to settings')).toBeInTheDocument();
  });

  it('test_401_routes_to_session_expired_view', async () => {
    api.post.mockRejectedValue(new Error('Unauthorized'));
    renderOpen();
    await waitForLoaded();

    await act(async () => { fireEvent.click(screen.getByText('Delete my data')); });
    await act(async () => {
      fireEvent.click(screen.getByText('Permanently delete everything'));
    });

    await waitFor(() => {
      expect(screen.getByText(/session has expired/i)).toBeInTheDocument();
    });
    expect(signOutSpy).not.toHaveBeenCalled();
  });
});
