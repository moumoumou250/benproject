import type { LoadedRun } from "@/data/serverSource";
export type ConsoleView = "overview" | "activity" | "history";
export interface ScanRequest {
  target: string;
  mode: string;
  instruction: string;
  budget: number;
  max_turns: number;
}
export interface ScanStatus {
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
