import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

/**
 * Sprint 5.2 §32 visual-QA harness config (temporary).
 * Serves preview/index.html with the real plugins so the harness renders
 * the exact production CSS pipeline. The extension build stays in
 * vite.config.ts untouched.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5199, strictPort: false },
});
