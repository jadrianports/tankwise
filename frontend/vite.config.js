import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // In `npm run dev`, forward the SPA's relative /api calls to the Django dev
    // server. In Docker, Nginx serves the same /api path, so the SPA code never
    // changes between dev and production. It always fetches a relative /api/route.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
