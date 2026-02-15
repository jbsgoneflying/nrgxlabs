/** @type {import('next').NextConfig} */
const nextConfig = {
  basePath: "/flow-monitor",
  transpilePackages: ["@kalshi-monitor/shared"],
  // Expose basePath to client-side code
  env: {
    NEXT_PUBLIC_BASE_PATH: "/flow-monitor",
  },
  async rewrites() {
    // API_URL is server-only (used by the rewrite proxy, not the browser)
    const apiUrl = process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:3100";
    return [
      {
        source: "/api/:path*",
        destination: `${apiUrl}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
