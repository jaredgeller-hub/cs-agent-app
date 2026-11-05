import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraftCard from '../src/components/chat/DraftCard';
import SettingsContext from '../src/contexts/SettingsContext';
import api from '../src/lib/api';

vi.mock('../src/lib/api', () => ({
  default: { post: vi.fn(), get: vi.fn() },
}));

let clipboardWriteTextMock;

beforeEach(() => {
  clipboardWriteTextMock = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: clipboardWriteTextMock },
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

function renderWithSettings(ui, { openSettings = vi.fn() } = {}) {
  const result = render(
    <SettingsContext.Provider value={{ openSettings }}>
      {ui}
    </SettingsContext.Provider>
  );
  return { openSettings, ...result };
}

function mockResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: () => Promise.resolve(body),
  };
}

describe('DraftCard — to/subject display', () => {
  it('test_displays_to_and_subject_for_email_draft_with_payload', () => {
    renderWithSettings(
      <DraftCard
        label="Email draft"
        content="Hi Derek, wanted to follow up on..."
        actionType="email_draft"
        actionPayload={{
          to: 'derek.chang@horizonedu.example',
          subject: 'Re: Expansion timing',
        }}
      />
    );

    expect(screen.getByText('TO')).toBeInTheDocument();
    expect(screen.getByText('derek.chang@horizonedu.example')).toBeInTheDocument();
    expect(screen.getByText('SUBJECT')).toBeInTheDocument();
    expect(screen.getByText('Re: Expansion timing')).toBeInTheDocument();
    expect(screen.getByText(/Hi Derek, wanted to follow up/)).toBeInTheDocument();
  });

  it('test_omits_to_subject_block_when_payload_missing_to', () => {
    renderWithSettings(
      <DraftCard
        label="Email draft"
        content="Legacy draft body without structured fields."
        actionType="email_draft"
        actionPayload={{ company_id: 'acme' }}
      />
    );

    expect(screen.queryByText('TO')).toBeNull();
    expect(screen.queryByText('SUBJECT')).toBeNull();
    expect(screen.getByText(/Legacy draft body/)).toBeInTheDocument();
  });
});

describe('DraftCard — response-shape branching', () => {
  it('test_sent_status_displays_sent_to_email', async () => {
    api.post.mockResolvedValue(
      mockResponse({
        status: 'sent',
        gmail_message_id: 'msg_abc',
        to: 'priya@beacon.example',
      })
    );

    renderWithSettings(
      <DraftCard
        label="Email draft"
        content="Draft body"
        actionType="email_draft"
        actionPayload={{ to: 'priya@beacon.example', subject: 'Re: Expansion' }}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Approve/ }));

    await waitFor(() => {
      expect(screen.getByText(/✓ Sent to priya@beacon.example/)).toBeInTheDocument();
    });
  });

  it('test_scope_revoked_status_displays_reconnect_cta_and_calls_openSettings_on_click', async () => {
    api.post.mockResolvedValue(
      mockResponse({
        status: 'scope_revoked',
        message: 'Your Google permission has expired. Please reconnect in Settings.',
      })
    );

    const { openSettings } = renderWithSettings(
      <DraftCard
        label="Email draft"
        content="Draft body"
        actionType="email_draft"
        actionPayload={{ to: 'priya@beacon.example', subject: 'Re: Expansion' }}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Approve/ }));

    await waitFor(() => {
      expect(screen.getByText(/Send failed — Google permission expired/)).toBeInTheDocument();
    });

    const reconnectBtn = screen.getByRole('button', { name: /Reconnect/ });
    expect(reconnectBtn).toBeInTheDocument();

    fireEvent.click(reconnectBtn);
    expect(openSettings).toHaveBeenCalledTimes(1);
  });

  it('test_success_status_preserves_copied_to_clipboard_behavior', async () => {
    api.post.mockResolvedValue(
      mockResponse({
        status: 'success',
        result: { action: 'email_draft_approved' },
      })
    );

    renderWithSettings(
      <DraftCard
        label="Email draft"
        content="Draft body goes here"
        actionType="email_draft"
        actionPayload={{ to: 'someone@x.example', subject: 'Hello' }}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Approve/ }));

    await waitFor(() => {
      expect(screen.getByText(/✓ Copied to clipboard!/)).toBeInTheDocument();
    });

    expect(clipboardWriteTextMock).toHaveBeenCalledTimes(1);
    expect(clipboardWriteTextMock).toHaveBeenCalledWith('Draft body goes here');
  });
});

describe('DraftCard — error message differentiation', () => {
  it('test_email_draft_error_does_not_say_failed_to_write_to_hubspot', async () => {
    api.post.mockRejectedValue(new Error('network down'));

    renderWithSettings(
      <DraftCard
        label="Email draft"
        content="Draft body"
        actionType="email_draft"
        actionPayload={{ to: 'priya@beacon.example', subject: 'Re: Expansion' }}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Approve/ }));

    await waitFor(() => {
      expect(screen.getByText(/Failed to save draft/)).toBeInTheDocument();
    });

    expect(screen.queryByText(/Failed to write to HubSpot/)).toBeNull();
  });
});
