import { LayoutDashboard, Activity, History, ShieldCheck, KeyRound } from "lucide-react";
import type { ConsoleView } from "../types";
import type { ReactNode } from "react";
const items = [
  { id: "overview", label: "测试工作台", icon: LayoutDashboard },
  { id: "activity", label: "实时运行", icon: Activity },
  { id: "history", label: "历史报告", icon: History },
  { id: "keys", label: "Key 池", icon: KeyRound },
] as const;
export function ConsoleSidebar({
  view,
  onChange,
  running,
  runCount,
  children,
}: {
  view: ConsoleView;
  onChange: (view: ConsoleView) => void;
  running: boolean;
  runCount: number;
  children?: ReactNode;
}) {
  return (
    <aside className="console-sidebar">
      <a className="sidebar-brand" href="/">
        <span className="brand-mark">
          <ShieldCheck size={25} />
        </span>
        <div>
          BEN<span>Security workspace</span>
        </div>
      </a>
      <p className="nav-caption">工作空间</p>
      <nav aria-label="主导航">
        {items.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            className={view === id ? "nav-active" : ""}
            aria-current={view === id ? "page" : undefined}
            onClick={() => onChange(id)}
          >
            <Icon size={18} />
            {label}
            {id === "activity" && running && <i className="live-dot" />}
            {id === "history" && <small>{runCount}</small>}
          </button>
        ))}
      </nav>
      {children}
      <div className="sidebar-bottom">
        <div className="workspace-health">
          <i className="live-dot" />
          <div>
            本地工作空间<small>使用当前环境的模型与沙箱</small>
          </div>
        </div>
        <span className="sidebar-version">BEN / WEBSITE TESTING</span>
      </div>
    </aside>
  );
}
