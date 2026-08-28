import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    // Dev-only. In the stack, nginx proxies /api to the api container.
    proxy: { "/api": "http://api:8000" },
  },
});
