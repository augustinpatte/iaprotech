/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        title: ["Syne", "sans-serif"],
        body:  ["DM Sans", "sans-serif"],
      },
      colors: {
        "proxy-bg":      "#08111e",
        "proxy-surface": "#0d1c2e",
        "proxy-panel":   "#122338",
        "proxy-card":    "#172d45",
        "proxy-border":  "#1e3a56",
        "proxy-accent":  "#1a6fcc",
        "proxy-lt":      "#2d8ff0",
        "proxy-dim":     "#1a3a5c",
        "proxy-text":    "#d4e4f4",
        "proxy-muted":   "#6a90b8",
        "proxy-faint":   "#3a5878",
      },
      keyframes: {
        float:     { "0%,100%": { transform: "translateY(0)" }, "50%": { transform: "translateY(-8px)" } },
        "fade-in": { from: { opacity: 0, transform: "translateY(6px)" }, to: { opacity: 1, transform: "translateY(0)" } },
        "dot-pulse": { "0%,100%": { opacity: 1 }, "50%": { opacity: .3 } },
      },
      animation: {
        float:     "float 3.6s ease-in-out infinite",
        "fade-in": "fade-in .25s ease both",
        "dot-pulse": "dot-pulse 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
}
