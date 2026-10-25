import { lazy, Suspense } from "react";
import { Navigate } from "react-router";
import { useSession } from "../app/session";

const Landing = lazy(() => import("./Landing"));

export function Home() {
  const { data: user, isLoading } = useSession();
  if (isLoading) return null;
  if (user) return <Navigate to="/systems" replace />;
  return (
    <Suspense fallback={null}>
      <Landing />
    </Suspense>
  );
}
