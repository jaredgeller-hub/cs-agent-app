/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import sri from 'vite-plugin-sri4'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    sri(),
  ],
  build: {
    outDir: 'dist',
  },
  server: {
    proxy: {
      '/chat':      'http://localhost:8000',
      '/app':       'http://localhost:8000',
      '/auth':      'http://localhost:8000',
      '/billing':   'http://localhost:8000',
      '/login':     'http://localhost:8000',
      '/logout':    'http://localhost:8000',
      '/assistant': 'http://localhost:8000',
      '/pilot':     'http://localhost:8000',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.js'],
    include: ['tests/**/*.test.{js,jsx,ts,tsx}'],
  },
})
