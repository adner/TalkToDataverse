import type { NextConfig } from "next";

process.env.COPILOTKIT_TELEMETRY_DISABLED ??= "true";

// Extra hosts allowed to open the dev server, e.g. a LAN address: SOT_DEV_ORIGINS=192.168.1.20
const extraOrigins = (process.env.SOT_DEV_ORIGINS ?? "").split(",").map((host) => host.trim()).filter(Boolean);

const nextConfig: NextConfig = {
  allowedDevOrigins: ["localhost", "127.0.0.1", ...extraOrigins],
  reactStrictMode: true,
  // The live page is recorded; keep the dev badge out of the picture.
  devIndicators: false,
};

export default nextConfig;
