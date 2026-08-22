/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Dark theme only — this is a wall-monitor dashboard.
        ink: {
          900: "#0a0c10",
          800: "#11141b",
          700: "#171b24",
          600: "#1e232e",
          500: "#2a3040",
        },
      },
      fontFamily: {
        sans: ["Inter", "Noto Sans Thai", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
