import "@copilotkit/react-core/v2/styles.css";
import "./styles.css";
import "./stage.css";

import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "SpeedOfThought · Live cognition field",
  description: "Watch voice intent become concurrent, observable Dataverse operations.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
