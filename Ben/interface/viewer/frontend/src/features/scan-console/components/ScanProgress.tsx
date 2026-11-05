import { useEffect, useState } from "react";
import { AgentRoster } from "./AgentRoster";
import { setScanPaused } from "../api";
import {
  Activity,
  ArrowUpRight,
  Bot,
  Check,
  Clock3,
  LoaderCircle,
  Terminal,
  AlertCircle,
} from "lucide-react";
import type { RunDetail, ScanStatus } from "../types";

const names: Record<string, string> = {
  exec_command: "执行目标检查",
  record_coverage: "记录测试覆盖",
  finish_scan: "生成测试报告",
  create_agent: "分配测试任务",
  think: "分析证据",
  save_threat_model: "整理威胁模型",
  web_search: "检索参考信息",
};
function time(value: string) {
  const date = new Date(value.replace(" UTC", "Z").replace(" ", "T"));
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleTimeString("zh-CN", { hour12: false });
}
export function ScanProgress({
  status,
  detail,
  pending,
  updatedAt,
}: {
  status: ScanStatus | null;
  detail: RunDetail | null;
  pending: boolean;
  updatedAt: Date | null;
}) {
  const [now, setNow] = useState(Date.now());
  const [controlling, setControlling] = useState(false);
  const [controlError, setControlError] = useState("");
  const [paused, setPaused] = useState(!!status?.paused);
  useEffect(() => { setPaused(!!status?.paused); }, [status?.paused, status?.run, status?.running]);
  async function togglePause() {
    if (controlling) return;
    setControlling(true);
    setControlError("");
    try {
      const result = await setScanPaused(!paused);
      setPaused(!!result.paused);
    }
    catch (error) { setControlError(error instanceof Error ? error.message : String(error)); }
    finally { setControlling(false); }
  }
  useEffect(() => {
    if (!status?.running) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [status?.running]);
  const running = pending || !!status?.running;
  const done =
    !running &&
    status?.exit_code === 0 &&
    (!detail || detail.summary.status === "completed");
  const failed =
    !running &&
    status?.exit_code != null &&
    (status.exit_code !== 0 ||
      (!!detail?.finished && detail.summary.status !== "completed"));
  const tools =
    detail?.transcript.events.filter((e) => e.type === "tool") ?? [];
  const last = tools.at(-1);
  const toolName = String(
    last?.data.tool_name ?? last?.data.name ?? "工具调用",
  );
  const reporting =
    running &&
    tools.some((e) => (e.data.tool_name ?? e.data.name) === "finish_scan");
  const stage = done ? 3 : reporting ? 2 : tools.length ? 1 : 0;
  const label = failed
    ? "任务异常退出"
    : done
      ? "测试已完成"
      : reporting
        ? "正在生成报告"
        : tools.length
          ? last?.data.status === "running"
            ? names[toolName] || toolName
            : "模型正在分析 · 等待下一步动作"
          : running
            ? "正在准备环境与模型"
            : "等待开始测试";
  const start = detail?.summary.startTime
    ? Date.parse(detail.summary.startTime)
    : NaN;
  const end = detail?.summary.endTime
    ? Date.parse(detail.summary.endTime)
    : now;
  const seconds = Number.isFinite(start)
    ? Math.max(0, Math.floor((end - start) / 1000))
    : 0;
  const usage = detail?.raw.llm_usage as
    | { requests?: number; total_tokens?: number }
    | undefined;
  const stages = ["准备环境", "模型分析与测试", "生成报告", "测试完成"];
  return (
    <section className="console-card progress-card">
      <div className="card-heading">
        <div>
          <h2>运行进度</h2>
          {status?.running && (
            <button type="button" className="secondary-button" disabled={controlling}
              onClick={() => void togglePause()}>
              {controlling ? "正在提交…" : paused ? "继续测试" : "暂停测试"}
            </button>
          )}
          {paused && <p>已请求暂停：当前步骤完成后，所有 Agent 将暂停；点击继续测试恢复。</p>}
          {controlError && <p role="alert" className="console-error">{controlError}</p>}
        </div>
        <span
          className={`state-badge ${running ? "is-live" : failed ? "is-failed" : ""}`}
        >
          {running ? (
            <>
              <i className="live-dot" />
              实时运行
            </>
          ) : done ? (
            "已完成"
          ) : failed ? (
            "异常退出"
          ) : (
            "待启动"
          )}
        </span>
      </div>
      <div className={`current-activity ${failed ? "activity-failed" : ""}`}>
        {running ? (
          <LoaderCircle className="spin" size={24} />
        ) : failed ? (
          <AlertCircle size={24} />
        ) : done ? (
          <Check size={24} />
        ) : (
          <Activity size={24} />
        )}
        <div>
          <strong>{label}</strong>
          <p>
            {status?.target ||
              "提交目标后，执行阶段和模型活动将在这里实时更新。"}
          </p>
        </div>
      </div>
      <ol className="stage-track" aria-label="测试执行阶段">
        {stages.map((name, i) => (
          <li
            key={name}
            className={
              status?.run || running
                ? i < stage || done
                  ? "stage-done"
                  : i === stage
                    ? failed
                      ? "stage-failed"
                      : "stage-current"
                    : ""
                : ""
            }
          >
            <span>{i < stage || done ? <Check size={13} /> : i + 1}</span>
            <small>{name}</small>
          </li>
        ))}
      </ol>
      <p className="stage-note">
        {failed
          ? `进程退出码 ${status?.exit_code}，请查看日志确认原因。`
          : running
            ? "阶段按实际执行事件更新；耗时取决于模型响应与测试范围。"
            : done
              ? "执行记录与报告已保存，可进入详情查看证据。"
              : "开始后自动刷新，无需手动重载页面。"}
      </p>
      <div className="execution-metrics">
        <div>
          <Clock3 size={15} />
          <strong>
            {Math.floor(seconds / 60)}
            <small>分</small>
            {seconds % 60}
            <small>秒</small>
          </strong>
          <span>已用时间</span>
        </div>
        <div>
          <Bot size={15} />
          <strong>{usage?.requests ?? 0}</strong>
          <span>模型请求</span>
        </div>
        <div>
          <Terminal size={15} />
          <strong>{tools.length}</strong>
          <span>工具调用</span>
        </div>
        <div>
          <Activity size={15} />
          <strong>{detail?.transcript.agents.length ?? 0}</strong>
          <span>Agent 数量</span>
        </div>
      </div>
      <div className="scan-run-columns">
        <AgentRoster
          agents={detail?.transcript.agents ?? []}
          events={detail?.transcript.events ?? []}
          runId={status?.run ?? null}
        />
        <div className="scan-run-activity">
          <div className="activity-heading">
            <h3>最近执行活动</h3>
            <small>
              {updatedAt
                ? `${updatedAt.toLocaleTimeString("zh-CN", { hour12: false })} 已同步`
                : "正在连接…"}
            </small>
          </div>
          <div className="activity-list">
            {tools.length ? (
              tools
                .slice(-6)
                .reverse()
                .map((event) => {
                  const tool = String(
                    event.data.tool_name ?? event.data.name ?? "工具调用",
                  );
                  const state = String(event.data.status ?? "");
                  return (
                    <div className="activity-row" key={event.id}>
                      <span
                        className={`activity-icon ${state === "running" ? "active" : ""}`}
                      >
                        {state === "running" ? (
                          <LoaderCircle className="spin" size={14} />
                        ) : (
                          <Check size={14} />
                        )}
                      </span>
                      <div>
                        <strong>{names[tool] || tool}</strong>
                        <small>
                          {detail?.transcript.agents.find(
                            (a) => a.id === event.agent_id,
                          )?.name ?? "Agent"}{" "}
                          · {tool}
                        </small>
                      </div>
                      <span>
                        {state === "running"
                          ? "执行中"
                          : state === "failed" || state === "error"
                            ? "失败"
                            : "已返回"}
                      </span>
                      <time>{time(event.timestamp)}</time>
                    </div>
                  );
                })
            ) : (
              <div className="activity-empty">
                <Bot size={24} />
                <p>
                  {running
                    ? "模型正在连接或生成响应；收到工具事件后会显示在这里。"
                    : "暂无执行活动，创建测试后即可查看。"}
                </p>
              </div>
            )}
          </div>
        </div>
      </div>
      {status?.run && (
        <a
          className="report-link"
          href={`/?view=run&run=${encodeURIComponent(status.run)}`}
        >
          查看完整执行过程与报告 <ArrowUpRight size={15} />
        </a>
      )}
      <details className="raw-log">
        <summary>查看原始运行日志</summary>
        <pre>{status?.log?.replace(/\x1b\[[0-9;]*m/g, "") || "暂无日志"}</pre>
      </details>
    </section>
  );
}
