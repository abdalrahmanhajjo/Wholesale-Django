/** @type {import('tailwindcss').Config} */
module.exports = {
  // static/js is scanned too. Tailwind removes any component class it cannot
  // find in the content files, so every class the scripts add at runtime —
  // .combo-native, .field-live-error, .step-index — was being compiled away.
  // That is why a searchable field rendered as two boxes: the rule that hides
  // the native <select> did not exist in the stylesheet.
  content: ["./templates/**/*.html", "./apps/**/*.py", "./static/js/**/*.js"],
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
        // Input boundaries are the only thing separating a field from the page,
        // so they carry the 3:1 non-text contrast requirement (WCAG 1.4.11).
        // The old #d7e3dc measured 1.32:1.
        "input-line": "#788d84",
        "muted-bg": "#f1f5f4",
        // Passes 4.5:1 on white *and* on muted-bg, where secondary text also
        // lands (table headers, filter chips). The old #667682 was 4.27:1 there.
        "muted-fg": "#54636f",
        // Sidebar secondary text sits on #071a2f; slate-500 measured 3.68:1.
        "nav-fg": "#cbd5e1",
        "nav-muted": "#8fa0b0",
        danger: "#dc2626",
        // #dc2626 on red-50 is 4.41:1, so message bodies use the darker step.
        "danger-strong": "#b91c1c"
      },
      fontFamily: {
        sans: ["Aptos", "Segoe UI", "system-ui", "sans-serif"]
      },
      borderRadius: { xl2: "0.8rem" }
    }
  },
  plugins: [require("@tailwindcss/forms")]
};
