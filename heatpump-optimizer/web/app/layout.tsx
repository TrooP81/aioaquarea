import "./globals.css";

export const metadata = {
  title: "Heat Pump Optimizer",
  description: "Cost-optimizing dashboard for Panasonic Aquarea heat pumps",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <a href="#overview" className="skip-link">Skip to content</a>
        {children}
      </body>
    </html>
  );
}
