/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./templates/**/*.html", "./apps/**/*.py"],
  theme: {
    extend: {
      colors: {
        ink: "#07172b",
        navy: "#07192d",
        "navy-2": "#0b2943",
        sidebar: "#07192d",
        brand: "#4bd27d",
        "brand-bright": "#66dc92",
        "brand-deep": "#1f7447",
        "brand-tint": "#edf9f1",
        paper: "#fbfcf9",
        canvas: "#f4f7f6",
        cream: "#f7f7f2",
        line: "#e1e9e7",
        "input-line": "#d7e3dc",
        "muted-bg": "#f1f5f4",
        "muted-fg": "#667682",
        danger: "#dc2626"
      },
      fontFamily: {
        sans: ["Aptos", "Segoe UI", "system-ui", "sans-serif"]
      },
      borderRadius: { xl2: "0.8rem" }
    }
  },
  plugins: [require("@tailwindcss/forms")]
};
