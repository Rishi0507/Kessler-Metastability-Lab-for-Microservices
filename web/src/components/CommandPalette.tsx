import { Command } from "cmdk";
import { useEffect } from "react";
import { useNavigate } from "react-router";
import { useSession, useSignOut } from "../app/session";
import { useCommands } from "../app/stores";
import { isDark, switchTheme } from "../app/theme";

export function CommandPalette() {
  const open = useCommands((s) => s.open);
  const setOpen = useCommands((s) => s.setOpen);
  const scoped = useCommands((s) => s.scoped);
  const setAuthOpen = useCommands((s) => s.setAuthOpen);
  const navigate = useNavigate();
  const { data: user } = useSession();
  const signOut = useSignOut();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(!useCommands.getState().open);
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setOpen]);

  if (!open) return null;
  const run = (fn: () => void) => () => {
    setOpen(false);
    fn();
  };
  const groups = new Map<string, typeof scoped[string]>();
  for (const cmds of Object.values(scoped)) {
    for (const c of cmds) groups.set(c.group, [...(groups.get(c.group) ?? []), c]);
  }

  return (
    <div className="cmdk-overlay" onMouseDown={(e) => e.target === e.currentTarget && setOpen(false)}>
      <Command className="cmdk" label="Command palette" loop>
        <Command.Input autoFocus placeholder="Type a command or search…" />
        <Command.List>
          <Command.Empty>No matching commands.</Command.Empty>
          {[...groups.entries()].map(([group, cmds]) => (
            <Command.Group key={group} heading={group}>
              {cmds.map((c) => (
                <Command.Item key={c.id} value={`${c.label} ${c.hint ?? ""}`} onSelect={run(c.run)}>
                  {c.label}
                  {c.hint && <span className="faint" style={{ marginLeft: "auto", fontSize: "var(--t-12)" }}>{c.hint}</span>}
                </Command.Item>
              ))}
            </Command.Group>
          ))}
          <Command.Group heading="Go to">
            <Command.Item onSelect={run(() => navigate("/systems"))}>Systems</Command.Item>
            <Command.Item onSelect={run(() => navigate("/status"))}>Platform status</Command.Item>
            <Command.Item onSelect={run(() => navigate("/"))}>Home</Command.Item>
          </Command.Group>
          <Command.Group heading="Preferences">
            <Command.Item onSelect={run(() => switchTheme(isDark() ? "light" : "dark"))}>Switch to {isDark() ? "light" : "dark"} theme</Command.Item>
            <Command.Item onSelect={run(() => switchTheme("system"))}>Follow the system theme</Command.Item>
          </Command.Group>
          <Command.Group heading="Account">
            {!user && <Command.Item onSelect={run(() => setAuthOpen(true))}>Sign in</Command.Item>}
            {user?.is_guest && <Command.Item onSelect={run(() => setAuthOpen(true))}>Save your work to an account</Command.Item>}
            {user && <Command.Item onSelect={run(() => signOut.mutate())}>Sign out</Command.Item>}
          </Command.Group>
        </Command.List>
      </Command>
    </div>
  );
}
