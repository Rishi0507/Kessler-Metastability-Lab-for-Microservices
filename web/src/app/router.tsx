import { lazy, Suspense, type ReactNode } from "react";
import { createBrowserRouter } from "react-router";
import { Shell } from "../components/Shell";
import { Home } from "../pages/Home";
import { NotFound } from "../pages/NotFound";

const SystemsPage = lazy(() => import("../pages/SystemsPage"));
const SystemPage = lazy(() => import("../pages/SystemPage"));
const AnalysisPage = lazy(() => import("../pages/AnalysisPage"));
const LiveRunPage = lazy(() => import("../pages/LiveRunPage"));
const StatusPage = lazy(() => import("../pages/StatusPage"));
const ReportPage = lazy(() => import("../pages/ReportPage"));

function Page({ children }: { children: ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="page" aria-busy="true">
          <div className="skeleton" style={{ height: 36, width: 280, marginBottom: 24 }} />
          <div className="skeleton" style={{ height: 420 }} />
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

export const router = createBrowserRouter([
  {
    element: <Shell />,
    children: [
      { index: true, element: <Home /> },
      { path: "systems", element: <Page><SystemsPage /></Page> },
      { path: "systems/:id", element: <Page><SystemPage /></Page> },
      { path: "analyses/:id", element: <Page><AnalysisPage /></Page> },
      { path: "runs/:id", element: <Page><LiveRunPage /></Page> },
      { path: "status", element: <Page><StatusPage /></Page> },
      { path: "r/:slug", element: <Page><ReportPage /></Page> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);
