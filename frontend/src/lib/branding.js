// Operator branding. VITE_COMPANY_NAME is inlined by Vite at BUILD time, so
// it must be set in the build environment (not only at runtime).
export const COMPANY_NAME = (import.meta.env.VITE_COMPANY_NAME || '').trim() || 'Your Company';
