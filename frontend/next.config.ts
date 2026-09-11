import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: process.env.NEXT_STANDALONE === "1" ? "standalone" : undefined,
};

export default nextConfig;
