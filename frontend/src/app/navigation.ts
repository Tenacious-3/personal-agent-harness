export const views = [
  "Chat",
  "Goals",
  "Memory",
  "History",
  "People",
  "Repairs",
  "Evidence",
  "Schedules",
  "Tasks",
  "Workflows",
  "Browser",
  "Approvals",
  "Inbox",
  "Files",
  "Connections",
  "Activity",
] as const;
export type View = (typeof views)[number];
export type Destination = { view: View; id?: string; kind?: string; error?: string };
export type Navigate = (view: View, id?: string, kind?: string) => void;

export function workspaceUrl(view: View, id?: string, kind?: string) {
  const parameters = new URLSearchParams();
  if (id) parameters.set("id", id);
  if (kind) parameters.set("kind", kind);
  return view === "Chat" && !parameters.size ? "/" : `/#/${view.toLowerCase()}${parameters.size ? `?${parameters}` : ""}`;
}

export function readDestination(): Destination {
  const hash = window.location.hash;
  if (!hash || hash === "#") return { view: "Chat" };
  const [path, search] = hash.slice(1).split(/\?(.*)/s, 2);
  const view = views.find((candidate) => `/${candidate.toLowerCase()}` === path);
  if (!view) return { view: "Chat", error: "This workspace page could not be found." };
  const parameters = new URLSearchParams(search);
  return { view, id: parameters.get("id") || undefined, kind: parameters.get("kind") || undefined };
}
export const navigationGroups: { label: string; items: readonly View[] }[] = [
  { label: "Everyday", items: ["Chat", "Inbox", "Approvals"] },
  { label: "Your work", items: ["Goals", "Tasks", "Workflows", "Schedules"] },
  { label: "Your knowledge", items: ["History", "Memory", "People", "Files"] },
  { label: "Tools & settings", items: ["Browser", "Connections", "Activity", "Repairs", "Evidence"] },
];
export const descriptions: Record<View, string> = {
  Chat: "A little context. A clear next step.",
  Goals: "A plan, its progress, and the next useful step.",
  Memory: "What your assistant knows, and why it knows it.",
  Schedules: "Work that starts at the right time.",
  Tasks: "Follow background work from request to result.",
  Browser: "A separate browser, with its own persistent profile.",
  Workflows: "Parallel specialists, a verifier, and one final answer.",
  Approvals: "Review the exact action before it happens.",
  Inbox: "Results and requests for your attention.",
  Files: "Your workspace and generated artifacts.",
  Connections: "Choose the services your assistant can access.",
  Activity: "A record of requests, decisions, and execution.",
  History: "Find past conversations, specialist work, and saved results in one place.",
  People: "Reviewed people, groups and relationships, with sources.",
  Repairs: "Track corrections and unresolved failures through review.",
  Evidence: "Inspect source freshness and claim-support decisions.",
};
