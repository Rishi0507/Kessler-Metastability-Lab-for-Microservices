import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import { api, ApiError } from "../lib/api";
import type { User } from "../lib/types";

export const useAfterSignIn = create<{ path: string | null; set: (path: string | null) => void }>((set) => ({
  path: null,
  set: (path) => set({ path }),
}));

export function useSession() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async (): Promise<User | null> => {
      try {
        return (await api.get<{ user: User }>("/api/v1/me")).user;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
  });
}

export function useGuestSignIn() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ user: User }>("/api/v1/auth/guest"),
    onSuccess: (r) => qc.setQueryData(["me"], r.user),
  });
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; password: string }) => api.post<{ user: User }>("/api/v1/auth/login", body),
    onSuccess: (r) => qc.setQueryData(["me"], r.user),
  });
}

export function useRegister() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; password: string; handle: string }) =>
      api.post<{ user: User }>("/api/v1/auth/register", body),
    onSuccess: (r) => qc.setQueryData(["me"], r.user),
  });
}

export function useSignOut() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<void>("/api/v1/auth/logout"),
    onSettled: () => {
      qc.clear();
      qc.setQueryData(["me"], null);
    },
  });
}
