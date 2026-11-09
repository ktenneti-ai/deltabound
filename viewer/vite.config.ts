import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "./", // dist/ works from any path, including file servers and sub-folders
  plugins: [react()],
  worker: { format: "es" },
  test: { environment: "node", include: ["test/**/*.test.ts"] },
});
