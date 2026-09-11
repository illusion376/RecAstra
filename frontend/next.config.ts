import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Docker-сборка (frontend/Dockerfile) собирает автономный server.js.
  // Локально остаётся обычный режим, чтобы `npm run start` работал как раньше.
  output: process.env.NEXT_STANDALONE === "1" ? "standalone" : undefined,
};

export default nextConfig;
