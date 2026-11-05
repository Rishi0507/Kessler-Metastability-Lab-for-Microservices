import { LogOut, Moon, Search, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet } from "react-router";
import { useSession, useSignOut } from "../app/session";
import { useCommands, useToasts } from "../app/stores";
import { applyTheme, isDark } from "../app/theme";
import { realtime, type ConnState } from "../lib/ws";
import { AuthDialog } from "./AuthDialog";
import { CommandPalette } from "./CommandPalette";
import { Wordmark } from "./ui";

function ConnectionBanner() {
  const [state, setState] = useState<ConnState>("idle");
  useEffect(() => realtime.onState(setState), []);
  if (state !== "reconnecting") return null;
  return (
    <div className="conn-banner" role="status">
      Live updates paused. Reconnecting…
    </div>
  );
}

function Toasts() {
  const toasts = useToasts((s) => s.toasts);
  const dismiss = useToasts((s) => s.dismiss);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} onClick={() => dismiss(t.id)} role={t.kind === "error" ? "alert" : "status"}>
          {t.text}
        </div>
      ))}
    </div>
  );
}

export function Shell() {
  const { data: user } = useSession();
  const signOut = useSignOut();
  const setOpen = useCommands((s) => s.setOpen);
  const setAuthOpen = useCommands((s) => s.setAuthOpen);
  const [dark, setDark] = useState(isDark());
  const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <>
      <header className="shell-bar">
        <Link to="/" className="home-link">
          <Wordmark />
        </Link>
        <nav className="nav" aria-label="Main">
          {user && <NavLink to="/systems">Systems</NavLink>}
          <NavLink to="/status">Status</NavLink>
        </nav>
        <div className="spacer" />
        <button className="btn btn-ghost icon-btn" onClick={() => setOpen(true)} aria-label="Search and commands" title={`Search and commands (${mac ? "⌘" : "Ctrl"} K)`}>
          <Search size={17} />
        </button>
        <button
          className="btn btn-ghost icon-btn"
          aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
          title={dark ? "Light theme" : "Dark theme"}
          onClick={() => {
            applyTheme(dark ? "light" : "dark");
            setDark(!dark);
          }}
        >
          {dark ? <Sun size={17} /> : <Moon size={17} />}
        </button>
        {user ? (
          <>
            {user.is_guest ? (
              <button className="btn btn-sm save-work" onClick={() => setAuthOpen(true)}>
                Save your work
              </button>
            ) : (
              <span className="handle">{user.handle}</span>
            )}
            <button className="btn btn-ghost icon-btn" onClick={() => signOut.mutate()} aria-label="Sign out" title="Sign out">
              <LogOut size={17} />
            </button>
          </>
        ) : (
          <button className="btn btn-sm" onClick={() => setAuthOpen(true)}>
            Sign in
          </button>
        )}
      </header>
      <main>
        <Outlet />
      </main>
      <ConnectionBanner />
      <CommandPalette />
      <AuthDialog />
      <Toasts />
    </>
  );
}
