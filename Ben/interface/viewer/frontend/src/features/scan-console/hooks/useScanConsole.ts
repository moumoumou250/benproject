import { useCallback, useEffect, useRef, useState } from "react";
import { fetchRunSummary, fetchTranscript } from "@/data/serverSource";
import { getHistory, getStatus, launchScan } from "../api";
import type { HistoryRun, RunDetail, ScanRequest, ScanStatus } from "../types";

export function useScanConsole(enabled: boolean) {
  const [status, setStatus] = useState<ScanStatus | null>(null);
  const [runs, setRuns] = useState<HistoryRun[]>([]);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [pending, setPending] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const revision = useRef(0);
  useEffect(() => {
    if (!enabled) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const poll = async () => {
      const generation = revision.current;
      try {
        const [state, history] = await Promise.all([
          getStatus(controller.signal),
          getHistory(controller.signal),
        ]);
        let loaded: RunDetail | null = null;
        const selected = new URLSearchParams(location.search).get("progress");
        const runName = selected || state.run;
        if (runName) {
          const [record, transcript] = await Promise.all([
            fetchRunSummary(runName),
            fetchTranscript(runName),
          ]);
          loaded = { ...record, transcript };
          if (selected) {
            state.run = selected;
            state.target = record.summary.targets[0] || selected;
            state.running =
              !record.finished && record.summary.status === "running";
            state.exit_code = record.finished
              ? record.summary.status === "completed"
                ? 0
                : 1
              : null;
          }
        }
        if (active && generation === revision.current) {
          setStatus(state);
          setRuns(history.runs);
          setDetail(loaded);
          setUpdatedAt(new Date());
          setConnectionError("");
        }
      } catch (e) {
        if (active)
          setConnectionError(e instanceof Error ? e.message : String(e));
      }
      if (active) timer = setTimeout(poll, 1500);
    };
    void poll();
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, [enabled]);
  const start = useCallback(async (request: ScanRequest) => {
    revision.current++;
    setPending(true);
    setError("");
    try {
      const state = await launchScan(request);
      setStatus(state);
      setDetail(null);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setPending(false);
    }
  }, []);
  return {
    status,
    runs,
    detail,
    error: error || connectionError,
    pending,
    updatedAt,
    start,
  };
}
