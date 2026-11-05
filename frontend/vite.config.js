import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

const page = (p) => fileURLToPath(new URL(p, import.meta.url));

// Two pages: "/" (employee punch page) and "/admin/" (admin portal).
export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: { input: { main: page("./index.html"), admin: page("./admin/index.html") } },
  },
  server: { port: 5173, host: true },
  preview: { port: 4173, host: true },
});
