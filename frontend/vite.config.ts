import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Сборка кладётся в dist/; корневой Dockerfile копирует её в backend/app/static,
// и API отдаёт её с корня. В разработке /api проксируется на локальный backend;
// SSE (/api/v1/stream) идёт через тот же прокси, websocket не нужен.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: { outDir: "dist" },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        // Если backend упал посреди SSE, vite оставляет ответ открытым (заголовки уже
        // ушли) и EventSource не узнаёт об обрыве. Закрываем ответ сами — поток
        // переподключится, как без прокси.
        configure(proxy) {
          proxy.on("error", (_err, _req, res) => {
            if ("headersSent" in res && res.headersSent && !res.writableEnded) res.end();
          });
        },
      },
    },
    // Словари импортируются из ../contracts: без разрешения dev-сервер их не отдаст.
    fs: { allow: [".", "../contracts"] },
  },
});
