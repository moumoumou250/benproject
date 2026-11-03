import { lazy, Suspense, useState } from "react";
import { Bot, ChevronRight, GitBranch } from "lucide-react";
import type { TranscriptAgent, TranscriptEvent } from "@/data/serverSource";

const AgentDetailModal = lazy(() => import("@/components/live/AgentDetailModal"));
const labels: Record<string, string> = { running: "运行中", waiting: "等待中", completed: "已完成", stopped: "已停止", crashed: "异常", failed: "失败", budget_paused: "预算暂停" };

export function AgentRoster({ agents, events, runId }: { agents: TranscriptAgent[]; events: TranscriptEvent[]; runId: string | null }) {
  const [selection, setSelection] = useState<{ run: string | null; id: string } | null>(null);
  const selected = selection?.run === runId ? agents.find(a => a.id === selection.id) ?? null : null;
  const ordered: { agent: TranscriptAgent; depth: number }[] = [];
  const seen = new Set<string>();
  function visit(agent: TranscriptAgent, depth: number) {
    if (seen.has(agent.id)) return;
    seen.add(agent.id); ordered.push({ agent, depth });
    agents.filter(a => a.parent_id === agent.id).forEach(a => visit(a, depth + 1));
  }
  agents.filter(a => !a.parent_id || !agents.some(parent => parent.id === a.parent_id)).forEach(a => visit(a, 0));
  agents.forEach(a => visit(a, 0));
  const children = agents.filter(a => a.parent_id);
  return <section className="agent-roster" aria-label="Agent 协作">
    <div className="activity-heading"><h3><GitBranch size={15}/> Agent 协作</h3><small>主 Agent {agents.length - children.length} · 子 Agent {children.length}</small></div>
    {!agents.length ? <p className="agent-roster-empty">任务启动后，将展示主 Agent 与子 Agent 的分工和执行状态。</p> : <>
      {!children.length && <p className="agent-roster-empty">当前为单 Agent 执行，尚未创建子 Agent。</p>}
      <div className="agent-tree">{ordered.map(({ agent, depth }) => {
        const mine = events.filter(e => e.agent_id === agent.id && e.type === "tool");
        const last = mine.at(-1);
        const parent = agents.find(a => a.id === agent.parent_id);
        return <button key={agent.id} type="button" className="agent-roster-row" style={{ marginLeft: Math.min(depth, 4) * 16 }} onClick={() => setSelection({ run: runId, id: agent.id })} aria-label={`查看 ${agent.name} 的执行详情`}>
          <span className={`agent-avatar ${agent.parent_id ? "child" : "root"}`}><Bot size={17}/></span>
          <span className="agent-roster-info"><strong>{agent.name}<small>{agent.parent_id ? "子 Agent" : "主 Agent"}</small></strong><span>{parent ? `上级：${parent.name} · ` : ""}{mine.length} 次工具调用</span><span>最近动作：{last ? String(last.data.tool_name ?? "工具调用") : "等待执行事件"}</span></span>
          <span className={`agent-status ${agent.status}`}>{labels[agent.status] ?? agent.status}</span><ChevronRight size={14}/>
        </button>;
      })}</div>
    </>}
    {selected && <Suspense fallback={<p role="status">正在加载 Agent 执行详情…</p>}><AgentDetailModal open agent={selected} events={events} steerable={false} onClose={() => setSelection(null)}/></Suspense>}
  </section>;
}
