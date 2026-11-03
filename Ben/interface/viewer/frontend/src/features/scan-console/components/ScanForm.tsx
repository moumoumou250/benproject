import { useState, type FormEvent } from "react";
import { ArrowRight } from "lucide-react";
import type { ScanRequest } from "../types";
export function ScanForm({
  busy,
  unavailable,
  onStart,
}: {
  busy: boolean;
  unavailable: boolean;
  onStart: (request: ScanRequest) => Promise<boolean>;
}) {
  const [target, setTarget] = useState("");
  const [mode, setMode] = useState("quick");
  const [instruction, setInstruction] = useState("");
  const [budget, setBudget] = useState("1");
  const [turns, setTurns] = useState("20");
  const [authorized, setAuthorized] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    await onStart({
      target,
      mode,
      instruction,
      budget: Number(budget),
      max_turns: Number(turns),
    });
  }
  return (
    <section className="console-card">
      <div className="card-heading">
        <div>
          <span className="eyebrow">NEW SCAN</span>
          <h2>创建网站测试</h2>
        </div>
        <span className="card-number">01</span>
      </div>
      <form onSubmit={submit}>
        <label>
          目标网站
          <input
            type="url"
            required
            placeholder="https://example.com"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            disabled={busy}
          />
        </label>
        <div className="field-row">
          <label>
            测试模式
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value)}
              disabled={busy}
            >
              <option value="quick">快速测试</option>
              <option value="standard">标准测试</option>
              <option value="deep">深度测试</option>
            </select>
          </label>
          <label>
            最大轮数
            <input
              type="number"
              min="1"
              max="200"
              required
              value={turns}
              onChange={(e) => setTurns(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            预算上限 / USD
            <input
              type="number"
              min="0.01"
              max="100"
              step="0.01"
              required
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              disabled={busy}
            />
          </label>
        </div>
        <label>
          测试要求 <span className="optional">可选</span>
          <textarea
            maxLength={12000}
            rows={5}
            placeholder="例如：仅做只读检查，重点检查公开 API 和信息泄露，不修改业务数据。"
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            disabled={busy}
          />
        </label>
        <label className="authorization">
          <input
            type="checkbox"
            checked={authorized}
            onChange={(e) => setAuthorized(e.target.checked)}
            required
            disabled={busy}
          />
          我确认有权对该目标进行所选范围的测试。
        </label>
        <button
          className="start-scan"
          disabled={busy || !authorized || unavailable}
        >
          {busy ? (
            "测试进行中…"
          ) : (
            <>
              启动测试 <ArrowRight size={16} />
            </>
          )}
        </button>
        <p className="field-note">
          使用本机模型与 Docker 环境 · 同时运行一个任务
        </p>
      </form>
    </section>
  );
}
