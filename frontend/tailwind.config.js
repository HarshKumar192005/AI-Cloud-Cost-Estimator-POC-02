/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/**/*.{js,jsx,ts,tsx}",
    "./components/**/*.{js,jsx,ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        azure: {
          DEFAULT: "#0078D4",
          light: "#E5F1FB",
        },
        oci: {
          DEFAULT: "#EE0000",
          light: "#FDEBEA",
        },
      },
    },
  },
  plugins: [],
};
