import { describe, it, expect, vi, afterEach } from 'vitest';

// VITE_COMPANY_NAME is inlined at build time; in tests vi.stubEnv sets it
// before the module is (re)imported.
describe('branding COMPANY_NAME', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('defaults to "Your Company" when VITE_COMPANY_NAME is unset', async () => {
    vi.stubEnv('VITE_COMPANY_NAME', '');
    vi.resetModules();
    const { COMPANY_NAME } = await import('../src/lib/branding');
    expect(COMPANY_NAME).toBe('Your Company');
  });

  it('defaults when VITE_COMPANY_NAME is blank', async () => {
    vi.stubEnv('VITE_COMPANY_NAME', '   ');
    vi.resetModules();
    const { COMPANY_NAME } = await import('../src/lib/branding');
    expect(COMPANY_NAME).toBe('Your Company');
  });

  it('uses VITE_COMPANY_NAME when set', async () => {
    vi.stubEnv('VITE_COMPANY_NAME', 'Acme');
    vi.resetModules();
    const { COMPANY_NAME } = await import('../src/lib/branding');
    expect(COMPANY_NAME).toBe('Acme');
  });
});
