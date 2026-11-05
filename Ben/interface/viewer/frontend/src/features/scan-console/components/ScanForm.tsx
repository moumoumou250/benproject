import { useState, type FormEvent } from "react";
import { ArrowRight } from "lucide-react";
import type { ScanRequest } from "../types";
import { TestRequirements } from "./TestRequirements";
import { composeRequirements, defaultRequirements } from "../requirements";
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
  const [requirements, setRequirements] = useState(defaultRequirements);
  const composed = composeRequirements(requirements, instruction);
  const tooLong = composed.length > 12000;
  const [budget, setBudget] = useState("1");
  const [turns, setTurns] = useState("20");
  const [authorized, setAuthorized] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (tooLong) return;
    await onStart({
      target,
      mode,
      instruction: composed,
      budget: Number(budget),
      max_turns: Number(turns),
    });
  }
  return (
    <section className="console-card">
      <div className="card-heading">
        <div>
          <h2>创建网站测试</h2>
        </div>
        <span className="card-number">01</span>
      </div>
      <form onSubmit={submit} className="scan-config-layout">
        <div>
          <label>
            目标网站
            <input
              type="text"
              inputMode="url"
              required
              placeholder="https://example.com 或 117.72.199.2:3000"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              disabled={busy}
            />
          </label>
          <p className="field-note">支持 IP、域名和完整网址；未填写协议时使用 http://，可指定端口。</p>
          <details className="scan-advanced">
            <summary>执行设置</summary>
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
          </details>
          <TestRequirements
            value={requirements}
            onChange={setRequirements}
            disabled={busy}
          />
          <label>
            补充要求 <span className="optional">可选</span>
            <textarea
              maxLength={4000}
              rows={5}
              placeholder="例如：仅做只读检查，重点检查公开 API 和信息泄露，不修改业务数据。"
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              disabled={busy}
            />
          </label>
        </div>
        <aside className="scan-config-summary">
          <h3>配置摘要</h3>
          <dl>
            <dt>目标网站</dt>
            <dd>{target.trim() || "待填写"}</dd>
            <dt>测试模式</dt>
            <dd>{{ quick: "快速", standard: "标准", deep: "深度" }[mode]}</dd>
            <dt>轮数 / 预算</dt>
            <dd>
              {turns || "—"} 轮 / {budget || "—"} USD
            </dd>
            <dt>检查重点</dt>
            <dd>{requirements.focus.join("、") || "未指定"}</dd>
            <dt>限定路径</dt>
            <dd>{requirements.paths.trim() || "未指定"}</dd>
            <dt>排除范围</dt>
            <dd>{requirements.excluded.trim() || "未指定"}</dd>
            <dt>操作 / 分工</dt>
            <dd>
              {requirements.behavior === "readonly" ? "只读检查" : "范围内验证"}{" "}
              · {requirements.agents === "single" ? "单 Agent" : "自动分工"}
            </dd>
          </dl>
          <details className="requirements-preview">
            <summary>
              预览实际提交的测试要求（{composed.length} / 12000 字符）
            </summary>
            <pre>{composed}</pre>
          </details>
          {tooLong && (
            <p className="console-error" role="alert">
              测试要求过长，请缩短内容后再提交。
            </p>
          )}
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
            disabled={busy || !authorized || unavailable || tooLong}
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
        </aside>
      </form>
    </section>
  );
}
