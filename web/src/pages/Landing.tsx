import { useRef, useState } from "react";
import { useAfterSignIn, useGuestSignIn } from "../app/session";
import { toastError, useCommands } from "../app/stores";
import { EnergyLandscape, type HeroPhase } from "../three/EnergyLandscape";
import { useScene } from "../three/useScene";

const CAPTIONS: Record<HeroPhase, string> = {
  resting: "A healthy system rests in a shallow basin.",
  "small-push": "A small trigger pushes it toward the ridge…",
  recovered: "…and it rolls back on its own. That is a recovery.",
  "big-push": "A bigger trigger at the same load…",
  stuck: "…carries it over. Now it stays down after the trigger is gone.",
};

export default function Landing() {
  const host = useRef<HTMLDivElement>(null);
  const [phase, setPhase] = useState<HeroPhase>("resting");
  useScene(host, (el) => {
    const s = new EnergyLandscape(el);
    s.onPhase = setPhase;
    return s;
  });
  const guest = useGuestSignIn();
  const setNext = useAfterSignIn((s) => s.set);
  const setAuthOpen = useCommands((s) => s.setAuthOpen);

  const tryIt = () => {
    setNext("/systems?start=checkout-retry-storm");
    guest.mutate(undefined, {
      onError: (e) => {
        setNext(null);
        toastError(e.message);
      },
    });
  };

  return (
    <div className="landing">
      <section className="hero">
        <div className="hero-copy">
          <h1 className="display">Your services have a second stable state. It is an outage.</h1>
          <p className="lede">
            Kessler simulates your timeouts, retries, queues and autoscaling under thousands of load and fault
            combinations, and shows exactly where a short incident turns into one that keeps itself going.
          </p>
          <div className="row">
            <button className="btn btn-primary btn-lg" onClick={tryIt} disabled={guest.isPending}>
              {guest.isPending ? "Setting up a workspace…" : "Try it on an example system"}
            </button>
            <button className="btn btn-lg" onClick={() => setAuthOpen(true)}>
              Sign in
            </button>
          </div>
          <p className="faint" style={{ fontSize: "var(--t-14)" }}>
            No signup needed for the example. Nothing is installed or connected to your infrastructure.
          </p>
        </div>
        <div className="hero-canvas" ref={host} aria-hidden="true">
          <p className="hero-caption" aria-live="polite">
            {CAPTIONS[phase]}
          </p>
        </div>
      </section>
      <section className="how" aria-label="How it works">
        <ol>
          <li>
            <span className="step">1. Describe</span>
            <p>
              Write your system in a short YAML file: services, timeouts, retries, budgets, breakers, queues and
              caches. Kessler flags risky settings as you type.
            </p>
          </li>
          <li>
            <span className="step">2. Sweep</span>
            <p>
              A deterministic C++ simulator runs your system across load levels and trigger sizes, and maps where it
              recovers, where it recovers slowly, and where it never comes back.
            </p>
          </li>
          <li>
            <span className="step">3. Fix and compare</span>
            <p>
              Apply the suggested fixes and re-run the same sweep with the same random seeds, so the difference you
              see comes from the fix alone.
            </p>
          </li>
        </ol>
      </section>
    </div>
  );
}
