import { lazy, useState } from "react";
import { ShieldCheck, ArrowRight } from "lucide-react";
const App = lazy(() => import("./App"));
import { ConsoleSidebar } from "./features/scan-console/components/ConsoleSidebar";
import { ScanForm } from "./features/scan-console/components/ScanForm";
import { ScanProgress } from "./features/scan-console/components/ScanProgress";
import { RunHistory } from "./features/scan-console/components/RunHistory";
import { useScanConsole } from "./features/scan-console/hooks/useScanConsole";
import type { ConsoleView, ScanRequest } from "./features/scan-console/types";
import "./scan-console.css";
import "./features/scan-console/console-layout.css";

const titles = {
  overview: ["测试工作台", "创建任务，并实时跟踪网站测试的每一步。"],
  activity: ["实时运行", "查看模型活动、工具调用与当前执行阶段。"],
  history: ["历史报告", "回顾测试结果，查看完整记录与证据。"],
};
export default function ScanConsole() {
  const viewing = new URLSearchParams(location.search).get("view") === "run";
  const [view, setView] = useState<ConsoleView>(() => {
    const value = location.hash.slice(1);
    return value === "activity" || value === "history" ? value : "overview";
  });
  const { status, runs, detail, error, pending, updatedAt, start } =
    useScanConsole(!viewing);
  function navigate(next: ConsoleView) {
    setView(next);
    const url = new URL(location.href);
    url.hash = next;
    if (next !== "activity") url.searchParams.delete("progress");
    history.replaceState(null, "", url);
  }
  async function launch(request: ScanRequest) {
    const ok = await start(request);
    if (ok) {
      const url = new URL(location.href);
      url.searchParams.delete("progress");
      history.replaceState(null, "", url);
      navigate("activity");
    }
    return ok;
  }
  if (viewing)
    return (
      <App />
    );
  return (
    <div className="scan-console">
      <ConsoleSidebar
        view={view}
        onChange={navigate}
        running={!!status?.running}
        runCount={runs.length}
      />
      <div className="console-workspace">
        <header className="console-topbar">
          <div>
            <span>工作空间</span>
            <ArrowRight size={12} />
            <strong>{titles[view][0]}</strong>
          </div>
          <span className="local-badge">
            <ShieldCheck size={13} />
            本地运行
          </span>
        </header>
        <main>
          <div className="console-title">
            <span className="eyebrow">WEBSITE SECURITY</span>
            <h1>{titles[view][0]}</h1>
            <p>{titles[view][1]}</p>
          </div>
          {error && (
            <div className="console-error" role="alert">
              连接或操作失败：{error}。已有数据保留，正在尝试重新同步。
            </div>
          )}
          {view === "overview" && (
            <>
              <div className="workspace-form">
                <ScanForm
                  busy={pending || !!status?.running}
                  unavailable={status?.available === false || !status}
                  onStart={launch}
                />
              </div>
              <RunHistory runs={runs.slice(0, 5)} />
            </>
          )}
          {view === "activity" && (
            <div className="activity-page">
              <label className="progress-run-picker">
                查看任务
                <select
                  aria-label="选择执行记录"
                  value={
                    new URLSearchParams(location.search).get("progress") || ""
                  }
                  onChange={(event) => {
                    const url = new URL(location.href);
                    if (event.target.value)
                      url.searchParams.set("progress", event.target.value);
                    else url.searchParams.delete("progress");
                    location.assign(url);
                  }}
                >
                  <option value="">当前任务</option>
                  {runs.map((run) => (
                    <option key={run.name} value={run.name}>
                      {run.target} · {run.name}
                    </option>
                  ))}
                </select>
              </label>
              <ScanProgress
                status={status}
                detail={detail}
                pending={pending}
                updatedAt={updatedAt}
              />
              {!status?.running && !status?.run && (
                <button
                  className="secondary-button"
                  onClick={() => navigate("overview")}
                >
                  创建一个测试任务 <ArrowRight size={15} />
                </button>
              )}
            </div>
          )}
          {view === "history" && <RunHistory runs={runs} searchable />}
        </main>
        <footer>BEN · 网站安全测试工作空间</footer>
      </div>
    </div>
  );
}
