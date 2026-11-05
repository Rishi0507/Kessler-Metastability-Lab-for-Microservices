import { lazy, Suspense } from "react";
import { Navigate } from "react-router";
import { useAfterSignIn, useSession } from "../app/session";

const Landing = lazy(() => import("./Landing"));

export function Home() {
  const { data: user, isLoading } = useSession();
  const next = useAfterSignIn((s) => s.path);
  if (isLoading) return null;
  if (user && next) return <Navigate to={next} replace />;
  return (
    <Suspense fallback={null}>
      <Landing />
    </Suspense>
  );
}
