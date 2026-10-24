import { useEffect } from "react";
import { create } from "zustand";

export interface Toast {
  id: number;
  text: string;
  kind: "info" | "error";
}

interface ToastState {
  toasts: Toast[];
  push: (text: string, kind?: Toast["kind"]) => void;
  dismiss: (id: number) => void;
}

let nextId = 1;

export const useToasts = create<ToastState>((set, get) => ({
  toasts: [],
  push: (text, kind = "info") => {
    const id = nextId++;
    set({ toasts: [...get().toasts.slice(-3), { id, text, kind }] });
    setTimeout(() => get().dismiss(id), kind === "error" ? 7000 : 4000);
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
}));

export const toast = (text: string) => useToasts.getState().push(text);
export const toastError = (text: string) => useToasts.getState().push(text, "error");

export interface Command {
  id: string;
  group: string;
  label: string;
  hint?: string;
  run: () => void;
}

interface CommandState {
  scoped: Record<string, Command[]>;
  set: (scope: string, cmds: Command[]) => void;
  clear: (scope: string) => void;
  open: boolean;
  setOpen: (v: boolean) => void;
  authOpen: boolean;
  setAuthOpen: (v: boolean) => void;
}

export const useCommands = create<CommandState>((set, get) => ({
  scoped: {},
  set: (scope, cmds) => set({ scoped: { ...get().scoped, [scope]: cmds } }),
  clear: (scope) => {
    const { [scope]: _, ...rest } = get().scoped;
    set({ scoped: rest });
  },
  open: false,
  setOpen: (v) => set({ open: v }),
  authOpen: false,
  setAuthOpen: (v) => set({ authOpen: v }),
}));

export function useRegisterCommands(scope: string, cmds: Command[]): void {
  const setCmds = useCommands((s) => s.set);
  const clear = useCommands((s) => s.clear);
  const key = cmds.map((c) => c.id).join("|");
  useEffect(() => {
    setCmds(scope, cmds);
    return () => clear(scope);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, key]);
}
