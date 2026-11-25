import { createContext, useContext, useSyncExternalStore } from "react";

const storageKey = "assistant.workspaceDrafts";
const storageNotice = "Drafts are available while this workspace stays open. Browser storage is unavailable, so a reload may lose them.";

export function createDraftStore(restore = true) {
  const listeners = new Set<() => void>();
  const drafts = new Map<string, string>();
  let warning = "";
  let closed = false;
  try {
    const raw = restore ? sessionStorage.getItem(storageKey) : null;
    if (raw) {
      for (const [key, value] of Object.entries(JSON.parse(raw))) {
        if (typeof value === "string") drafts.set(key, value);
      }
    }
  } catch {
    warning = storageNotice;
  }

  const notify = () => listeners.forEach((listener) => listener());
  return {
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    read(key: string) { return drafts.get(key); },
    warning() { return warning; },
    write(key: string, value?: string) {
      if (closed) return;
      if (value === undefined) drafts.delete(key);
      else drafts.set(key, value);
      try {
        if (drafts.size) sessionStorage.setItem(storageKey, JSON.stringify(Object.fromEntries(drafts)));
        else sessionStorage.removeItem(storageKey);
        warning = "";
      } catch {
        warning = storageNotice;
      }
      notify();
    },
    close() {
      closed = true;
      drafts.clear();
      try { sessionStorage.removeItem(storageKey); } catch { /* Storage may be blocked. */ }
      notify();
    },
  };
}

export const DraftContext = createContext<ReturnType<typeof createDraftStore> | null>(null);

export function useDraft(key: string) {
  const store = useContext(DraftContext);
  if (!store) throw new Error("Workspace drafts require a connected workspace");
  const value = useSyncExternalStore(store.subscribe, () => store.read(key));
  const warning = useSyncExternalStore(store.subscribe, store.warning);
  return { value, warning, write: (text?: string) => store.write(key, text) };
}
