/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://api:8500/api/:path*",
      },
    ];
  },
};

module.exports = nextConfig;
