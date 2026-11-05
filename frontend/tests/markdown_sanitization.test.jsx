/**
 * Regression guard: every ReactMarkdown surface must sanitize raw HTML and
 * javascript: URLs out of the rendered DOM.
 *
 * Three sanitized surfaces in production:
 *   - ChatWindow.AgentBubble  → LLM responses
 *   - DigestMessage           → morning digest
 *   - AlertMessage            → proactive alerts
 *
 * The fix landed Apr 16 in commit 4ab8bf2 (rehype-sanitize plugin added to
 * all three). This test exists so the plugin can't be silently dropped.
 *
 * Promotion path: this is the first frontend test in the repo. Day 3 CI
 * will pick up `npm run test` from frontend/package.json and run vitest
 * across `tests/**\/*.test.{js,jsx,ts,tsx}`.
 */

import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import ChatWindow from '../src/components/chat/ChatWindow';
import DigestMessage from '../src/components/chat/DigestMessage';
import AlertMessage from '../src/components/chat/AlertMessage';

const MALICIOUS_PAYLOADS = [
  '<script>window.__pwned = true;</script>',
  '<img src=x onerror="window.__pwned = true">',
  '<a href="javascript:alert(1)">click</a>',
  '![logo](javascript:alert(1))',
  '<iframe src="javascript:alert(1)"></iframe>',
  '<svg/onload="window.__pwned = true">',
  '<details ontoggle="window.__pwned = true" open>x</details>',
];

const COMBINED_PAYLOAD = MALICIOUS_PAYLOADS.join('\n\n');

/**
 * Assert the rendered container has no script tags, no event-handler
 * attributes, and no javascript: URLs.
 */
function assertSanitized(container) {
  expect(container.querySelectorAll('script').length).toBe(0);
  expect(container.querySelectorAll('iframe').length).toBe(0);

  for (const el of container.querySelectorAll('*')) {
    for (const attr of el.attributes) {
      // Block all event handlers (onerror, onclick, onload, ontoggle, ...)
      expect(attr.name.startsWith('on')).toBe(false);
      // Block javascript: in any href/src/xlink:href
      const v = attr.value || '';
      expect(/^\s*javascript:/i.test(v)).toBe(false);
    }
  }

  // Defense in depth: if a payload somehow executed during render, it
  // would have set window.__pwned. Confirm it's untouched.
  expect(window.__pwned).toBeUndefined();
}

describe('ReactMarkdown sanitization', () => {
  it('ChatWindow strips XSS from LLM responses', () => {
    delete window.__pwned;
    const messages = [
      { id: 'm1', role: 'agent', content: COMBINED_PAYLOAD, isError: false },
    ];
    const { container } = render(<ChatWindow messages={messages} loading={false} />);
    assertSanitized(container);
  });

  it('DigestMessage strips XSS from digest content', () => {
    delete window.__pwned;
    const { container } = render(<DigestMessage content={COMBINED_PAYLOAD} />);
    assertSanitized(container);
  });

  it('AlertMessage strips XSS from alert content', () => {
    delete window.__pwned;
    const { container } = render(<AlertMessage content={COMBINED_PAYLOAD} />);
    assertSanitized(container);
  });

  it('safe markdown still renders (sanitization is not over-aggressive)', () => {
    const safe = '**bold** *italic* [link](https://example.com) `code`';
    const { container } = render(<DigestMessage content={safe} />);
    expect(container.querySelector('strong')).not.toBeNull();
    expect(container.querySelector('em')).not.toBeNull();
    expect(container.querySelector('a')?.getAttribute('href')).toBe('https://example.com');
    expect(container.querySelector('code')).not.toBeNull();
  });

  it.each(MALICIOUS_PAYLOADS)('individual payload sanitized: %s', (payload) => {
    delete window.__pwned;
    const { container } = render(<DigestMessage content={payload} />);
    assertSanitized(container);
  });
});
