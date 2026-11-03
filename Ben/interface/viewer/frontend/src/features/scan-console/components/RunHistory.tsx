import { ArrowUpRight, Globe, FileClock } from "lucide-react";
import type { HistoryRun } from "../types";
export function RunHistory({ runs }: { runs: HistoryRun[] }) {
  return (
    <section className="console-card history-card">
      <div className="card-heading">
        <div>
          <span className="eyebrow">SCAN HISTORY</span>
          <h2>测试记录</h2>
        </div>
        <span className="count-pill">{runs.length} 次测试</span>
      </div>
      {runs.length ? (
        <div className="run-list">
          {runs.map((run) => (
            <a
              key={run.name}
              href={`/?view=run&run=${encodeURIComponent(run.name)}`}
            >
              <Globe size={18} />
              <div>
                <strong>{run.target || run.name}</strong>
                <small>{run.name}</small>
              </div>
              <span className={`history-status ${run.status}`}>
                {(
                  {
                    completed: "已完成",
                    running: "运行中",
                    failed: "失败",
                    stopped: "已停止",
                  } as Record<string, string>
                )[run.status] ||
                  run.status ||
                  "未知"}
              </span>
              <ArrowUpRight size={16} />
            </a>
          ))}
        </div>
      ) : (
        <div className="empty-state">
          <FileClock size={30} />
          <h3>还没有测试记录</h3>
          <p>创建第一个任务后，可在这里查看报告与执行证据。</p>
        </div>
      )}
    </section>
  );
}
