import type { ScanRequest, ScanStatus, HistoryRun } from "./types";
async function json<T>(
  url: string,
  signal?: AbortSignal,
  request?: RequestInit,
): Promise<T> {
  const response = await fetch(url, { ...request, signal, cache: "no-store" });
  const body = await response.json();
  if (!response.ok)
    throw new Error(
      body.error === "forbidden"
        ? "会话已失效，请使用启动终端的完整链接重新打开。"
        : body.error || `请求失败 (${response.status})`,
    );
  return body as T;
}
export const getStatus = (signal: AbortSignal) =>
  json<ScanStatus>("/api/scans", signal);
export const getHistory = (signal: AbortSignal) =>
  json<{ runs: HistoryRun[] }>("/api/runs", signal);
export const launchScan = (body: ScanRequest) =>
  json<ScanStatus>("/api/scans", undefined, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
