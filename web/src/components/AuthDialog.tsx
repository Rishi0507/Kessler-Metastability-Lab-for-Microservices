import { useState } from "react";
import { useNavigate } from "react-router";
import { useGuestSignIn, useLogin, useRegister, useSession } from "../app/session";
import { useCommands } from "../app/stores";
import { ApiError } from "../lib/api";
import { Dialog } from "./ui";

export function AuthDialog() {
  const open = useCommands((s) => s.authOpen);
  const setOpen = useCommands((s) => s.setAuthOpen);
  const { data: user } = useSession();
  const [mode, setMode] = useState<"login" | "register">(user?.is_guest ? "register" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [handle, setHandle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const login = useLogin();
  const register = useRegister();
  const guest = useGuestSignIn();
  const navigate = useNavigate();
  const busy = login.isPending || register.isPending || guest.isPending;

  const done = () => {
    setOpen(false);
    setError(null);
    setPassword("");
    navigate("/systems");
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      if (mode === "login") await login.mutateAsync({ email, password });
      else await register.mutateAsync({ email, password, handle });
      done();
    } catch (err) {
      const e2 = err as ApiError;
      setError(e2.errors?.[0]?.message ?? e2.detail ?? e2.message);
    }
  };

  const upgrading = user?.is_guest && mode === "register";
  return (
    <Dialog
      open={open}
      onClose={() => setOpen(false)}
      title={upgrading ? "Save your work" : mode === "login" ? "Sign in" : "Create an account"}
    >
      {upgrading && <p className="muted">Your guest systems and analyses move to the new account.</p>}
      <form onSubmit={submit} className="stack">
        <div className="field">
          <label htmlFor="auth-email">Email</label>
          <input id="auth-email" className="input" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        {mode === "register" && (
          <div className="field">
            <label htmlFor="auth-handle">Handle</label>
            <input id="auth-handle" className="input" required pattern="[a-z0-9][a-z0-9_-]{2,31}" value={handle} onChange={(e) => setHandle(e.target.value.toLowerCase())} />
            <span className="hint">3-32 characters: lowercase letters, digits, - or _</span>
          </div>
        )}
        <div className="field">
          <label htmlFor="auth-password">Password</label>
          <input
            id="auth-password"
            className="input"
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            minLength={mode === "register" ? 10 : 1}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {mode === "register" && <span className="hint">At least 10 characters.</span>}
        </div>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="row" style={{ justifyContent: "space-between" }}>
          <button type="button" className="btn btn-ghost" onClick={() => setMode(mode === "login" ? "register" : "login")}>
            {mode === "login" ? "Create an account instead" : "I already have an account"}
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {mode === "login" ? "Sign in" : upgrading ? "Save account" : "Create account"}
          </button>
        </div>
      </form>
      {!user && (
        <p className="faint" style={{ fontSize: "var(--t-14)" }}>
          Just looking?{" "}
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={async () => {
              await guest.mutateAsync();
              done();
            }}
          >
            Continue as a guest
          </button>
        </p>
      )}
    </Dialog>
  );
}
