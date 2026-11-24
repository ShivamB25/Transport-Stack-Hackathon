import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { defineConfig } from 'vite'

const api = 'http://127.0.0.1:3100'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    port: 3110,
    proxy: { '/journeys': api, '/health': api, '/live': api, '/replay': api, '/docs': api, '/openapi.json': api },
  },
})
