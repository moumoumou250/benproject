import { ArrowUpRight, Globe, FileClock } from "lucide-react";
import { useState } from "react";
import type { HistoryRun } from "../types";
export function RunHistory({
  runs,
  searchable = false,
}: {
  runs: HistoryRun[];
  searchable?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const matches = runs.filter(
    (run) =>
      (!status || run.status === status) &&
      `${run.target ?? ""} ${run.name}`
        .toLowerCase()
        .includes(query.trim().toLowerCase()),
  );
  return (
    <section className="console-card history-card">
      <div className="card-heading">
        <div>
          <h2>测试记录</h2>
        </div>
        <span className="count-pill">{runs.length} 次测试</span>
      </div>
      {searchable && runs.length > 0 && (
        <div className="field-row history-filters">
          <label>
            搜索任务
            <input
              type="search"
              placeholder="目标网址或任务名称"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <label>
            任务状态
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value)}
            >
              <option value="">全部状态</option>
              <option value="running">运行中</option>
              <option value="completed">已完成</option>
              <option value="failed">失败</option>
              <option value="stopped">已停止</option>
            </select>
          </label>
        </div>
      )}
      {matches.length ? (
        <div className="run-list">
          {matches.map((run) => (
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
          <h3>{runs.length ? "没有匹配的测试记录" : "还没有测试记录"}</h3>
          <p>
            {runs.length
              ? "试试其他关键词或状态。"
              : "创建第一个任务后，可在这里查看报告与执行证据。"}
          </p>
        </div>
      )}
    </section>
  );
}
