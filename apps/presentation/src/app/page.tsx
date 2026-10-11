"use client";

import dynamic from "next/dynamic";

const Dashboard = dynamic(
  () => import("@/components/dashboard").then((module) => module.Dashboard),
  { ssr: false },
);

export default function Home() {
  return <Dashboard />;
}
