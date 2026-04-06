import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => ({
  plugins: [
    react(),
    mode === 'development' && {
      name: 'dev-html-transform',
      transformIndexHtml(html) {
        return html
          .replace('<title>Fishtank Dashboard</title>', '<title>Fishtank Dashboard [DEV]</title>')
          .replace('href="/favicon.svg"', 'href="/dev-favicon.svg"')
      },
    },
  ].filter(Boolean),
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'recharts-vendor': ['recharts'],
          'lucide-vendor': ['lucide-react'],
          'virtuoso-vendor': ['react-virtuoso'],
        },
      },
    },
  },
  server: {
    port: 3000,
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
}));
