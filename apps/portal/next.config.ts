import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle for the Dockerfile runner stage.
  output: "standalone",
  reactStrictMode: true,
  // ESLint runs via `npm run lint` (Makefile `test-ts`), not during builds.
  eslint: { ignoreDuringBuilds: true },
};

export default nextConfig;
