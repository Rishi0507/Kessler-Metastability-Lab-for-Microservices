import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { useAfterSignIn, useGuestSignIn, useSession } from "../app/session";
import { toastError, useCommands } from "../app/stores";
import { EnergyLandscape, type HeroPhase } from "../three/EnergyLandscape";
import { InfoTip } from "../components/ui";
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
  const { data: user } = useSession();
  const navigate = useNavigate();
  const guest = useGuestSignIn();
  const setNext = useAfterSignIn((s) => s.set);
  const setAuthOpen = useCommands((s) => s.setAuthOpen);

  const tryIt = () => {
    if (user) {
      navigate("/systems?start=checkout-retry-storm");
      return;
    }
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
          <p className="lede">See where a short incident turns into one that never ends.</p>
          <div className="row">
            {user ? (
              <>
                <Link className="btn btn-primary btn-lg" to="/systems">
                  Open your systems
                </Link>
                <button className="btn btn-lg" onClick={tryIt}>
                  Try an example
                </button>
              </>
            ) : (
              <>
                <button className="btn btn-primary btn-lg" onClick={tryIt} disabled={guest.isPending}>
                  {guest.isPending ? "Setting up…" : "Try an example"}
                </button>
                <button className="btn btn-lg" onClick={() => setAuthOpen(true)}>
                  Sign in
                </button>
              </>
            )}
            {!user && (
              <InfoTip label="About the example">
                No signup needed. Kessler simulates a sample checkout service; nothing touches your infrastructure.
              </InfoTip>
            )}
          </div>
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
            <span className="step">1</span>
            <p>Describe your services in YAML</p>
          </li>
          <li>
            <span className="step">2</span>
            <p>Map where they stop recovering</p>
          </li>
          <li>
            <span className="step">3</span>
            <p>Apply a fix and compare</p>
          </li>
        </ol>
      </section>
    </div>
  );
}
