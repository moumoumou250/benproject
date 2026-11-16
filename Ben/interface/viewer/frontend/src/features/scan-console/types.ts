import type { LoadedRun } from "@/data/serverSource";
export type ConsoleView = "overview" | "activity" | "history" | "keys";
export interface ScanRequest {
  target: string;
  mode: string;
  instruction: string;
  budget: number;
  max_turns: number;
}
export interface ScanStatus {
  paused?: boolean;
  running: boolean;
  exit_code: number | null;
  run: string | null;
  target: string;
  log: string;
  available?: boolean;
}
export interface HistoryRun {
  name: string;
  target: string;
  status: string;
}
export type RunDetail = Pick<
  LoadedRun,
  "raw" | "summary" | "transcript" | "finished"
>;
export type KeyTier = "root" | "flash";
export interface KeyNodeStatus {
  tier: KeyTier;
  model: string | null;
  vendor: string | null;
  key_tail: string | null;
  base_url: string | null;
  subscription: boolean;
  source: "keypool" | "env";
  entry_id: string | null;
  entry_label: string | null;
}
export interface KeyPoolEntry {
  id: string;
  tier: KeyTier;
  label: string | null;
  model: string | null;
  vendor: string | null;
  key_tail: string | null;
  base_url: string | null;
  added_at: string | null;
  last_status: "ok" | "fail" | null;
  last_checked_at: string | null;
  last_error: string | null;
  active: boolean;
}
export interface KeyPoolPayload {
  root: KeyNodeStatus;
  flash: KeyNodeStatus;
  entries: KeyPoolEntry[];
  pool_path: string;
}
export type KeyPoolAction =
  | { action: "add"; tier: KeyTier; model: string; api_key?: string; base_url?: string; label?: string }
  | { action: "switch"; id: string }
  | { action: "clear"; tier: KeyTier }
  | { action: "delete"; id: string }
  | { action: "test"; id: string };
export interface KeyPoolActionResult {
  ok?: boolean;
  entry?: KeyPoolEntry;
  id?: string;
  status?: "ok" | "fail";
  detail?: string;
}
