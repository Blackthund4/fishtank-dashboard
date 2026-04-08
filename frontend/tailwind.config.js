/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        tank: {
          bg: "#0a0a0c",
          panel: "#111115",
          surface: "#111115",
          border: "#1e1e24",
          accent: "#00ff88",
          warn: "#ff4444",
          danger: "#ff4444",
          amber: "#ffaa00",
          dim: "#555566",
          muted: "#666677",
          highlight: "#1a1a22",
          text: "#c8c8d0",
          bright: "#e0e0e8",
        },
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', '"Fira Code"', "monospace"],
        display: ['"Outfit"', "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
