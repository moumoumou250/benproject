import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { AlertTriangle, Bot, LayoutDashboard } from "lucide-react";
import type { McpConnectionStatus } from "@/data/serverSource";
import type { View } from "@/App";
import { ConsoleSidebar } from "@/features/scan-console/components/ConsoleSidebar";

interface SidebarProps {
  view: View;
  onSelectView: (view: View) => void;
  issuesCount: number;
  agentCount: number;
  mcpConnections: McpConnectionStatus[];
  mcpInUse: Set<string>;
  runCount: number;
  finished: boolean;
  onOpenHistory: () => void;
}

export default function Sidebar(props: SidebarProps) {
  const items = [
    { id: "overview", label: "任务概览", icon: LayoutDashboard, count: undefined },
    { id: "issues", label: "漏洞详情", icon: AlertTriangle, count: props.issuesCount },
    { id: "agents", label: "Agent 活动", icon: Bot, count: props.agentCount },
  ] as const;
  return (
    <ConsoleSidebar
      view="history"
      onChange={(view) => {
        const url = new URL(location.href);
        url.searchParams.delete("view");
        url.searchParams.delete("run");
        url.searchParams.delete("progress");
        url.hash = view;
        location.assign(url);
      }}
      running={!props.finished}
      runCount={props.runCount}
    >
      <p className="nav-caption">当前任务</p>
      <nav aria-label="任务导航">
        {items.map(({ id, label, icon: Icon, count }) => (
          <button key={id} className={props.view === id ? "nav-active" : ""}
            aria-current={props.view === id ? "page" : undefined}
            onClick={() => props.onSelectView(id)}>
            <Icon size={18} />{label}{count != null && <small>{count}</small>}
          </button>
        ))}
      </nav>
      {props.mcpConnections.length > 0 && (
        <McpConnectionsPanel connections={props.mcpConnections} inUse={props.mcpInUse} />
      )}
    </ConsoleSidebar>
  );
}

// The quarter-circle sweep frames the terminal UI cycles for an in-use
// connection, and the sub-second tick that advances them.
const SWEEP_FRAMES = ["◐", "◓", "◑", "◒"] as const;
const SWEEP_MS = 220;

/**
 * The MCP connections panel: a compact roster of the run's connected MCP
 * servers, matching the terminal UI's sidebar panel. A header carries the
 * total count; each row shows a status glyph, the connection name, and its
 * tool count (or "offline"):
 *   - solid green dot: attached and idle;
 *   - green cycling quarter-circle (◐◓◑◒): a tool call is running against it;
 *   - red dot + "offline": the connection's live session has died.
 *
 * "In use" is derived by the caller from the connection-tagged tool events, not
 * carried on the roster, so a call in flight shows motion with no extra signal.
 * The roster scrolls within a bounded height so a long list never blows out the
 * rail, mirroring how the nav above it scrolls.
 */
function McpConnectionsPanel({
  connections,
  inUse,
}: {
  connections: McpConnectionStatus[];
  inUse: Set<string>;
}) {
  const anyInUse = connections.some((c) => !c.dead && inUse.has(c.name));
  const [frame, setFrame] = useState(0);

  // Advance the sweep only while at least one connection is in use, so an idle
  // panel does no work.
  useEffect(() => {
    if (!anyInUse) return;
    const id = setInterval(() => setFrame((f) => (f + 1) % SWEEP_FRAMES.length), SWEEP_MS);
    return () => clearInterval(id);
  }, [anyInUse]);

  return (
    <div className="mt-1">
      <div className="flex h-7 items-center px-2 text-[11px] font-medium text-[#666]">
        MCP Connections ({connections.length})
      </div>
      <div className="max-h-48 overflow-y-auto overflow-x-clip scrollbar-thin">
        {connections.map((conn) => {
          const busy = !conn.dead && inUse.has(conn.name);
          return (
            <div
              key={conn.name}
              className="flex h-7 items-center gap-2 rounded-md px-2"
              title={conn.provider ? `${conn.name} · ${conn.provider}` : conn.name}
            >
              <span
                className={cn(
                  "w-3 flex-none text-center text-[11px] leading-none",
                  conn.dead ? "text-red-400" : "text-emerald-400"
                )}
                aria-hidden="true"
              >
                {conn.dead ? "●" : busy ? SWEEP_FRAMES[frame] : "●"}
              </span>
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[#ededed]">
                {conn.name}
              </span>
              {conn.dead ? (
                <span className="flex-none text-[11px] text-red-400">offline</span>
              ) : (
                <span className="flex-none text-[11px] tabular-nums text-[#666]">
                  {conn.toolCount} {conn.toolCount === 1 ? "tool" : "tools"}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Overview icon: a dashboard grid glyph (16x16 viewBox).
function ProjectsIcon() {
  return (
    <svg style={{ width: 16, height: 16, color: "currentcolor" }} viewBox="0 0 16 16" fill="currentColor">
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M2.5 5.5V2.5H5.5V5.5H2.5ZM1 2C1 1.44772 1.44772 1 2 1H6C6.55228 1 7 1.44772 7 2V6C7 6.55228 6.55228 7 6 7H2C1.44772 7 1 6.55228 1 6V2ZM2.5 13.5V10.5H5.5V13.5H2.5ZM1 10C1 9.44772 1.44772 9 2 9H6C6.55228 9 7 9.44772 7 10V14C7 14.5523 6.55228 15 6 15H2C1.44772 15 1 14.5523 1 14V10ZM10.5 2.5V5.5H13.5V2.5H10.5ZM10 1C9.44772 1 9 1.44772 9 2V6C9 6.55228 9.44772 7 10 7H14C14.5523 7 15 6.55228 15 6V2C15 1.44772 14.5523 1 14 1H10ZM10.5 13.5V10.5H13.5V13.5H10.5ZM9 10C9 9.44772 9.44772 9 10 9H14C14.5523 9 15 9.44772 15 10V14C15 14.5523 14.5523 15 14 15H10C9.44772 15 9 14.5523 9 14V10Z"
      />
    </svg>
  );
}
