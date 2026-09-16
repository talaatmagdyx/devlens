import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const API_ROUTES = [
  "/health", "/ready", "/capabilities", "/auth", "/projects", "/settings",
  "/audit", "/jobs", "/analyses", "/intent", "/dashboard", "/integrations",
  "/approvals", "/proposals", "/search", "/notifications", "/services",
  "/knowledge", "/onboarding", "/capacity",
];

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: Object.fromEntries(
      API_ROUTES.map((route) => [route, "http://127.0.0.1:8000"]),
    ),
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // The e2e/ directory belongs to Playwright, which drives a real browser.
    // Vitest picking it up produces a confusing "test() called here" failure.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
});
