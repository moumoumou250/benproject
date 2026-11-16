import { useCallback, useEffect, useState } from "react";
import { KeyRound, Plus, RefreshCw, Trash2 } from "lucide-react";
import { getKeyPool, keyPoolAction } from "../api";
import type {
  KeyPoolEntry,
  KeyPoolPayload,
  KeyTier,
} from "../types";

const TIER_LABELS: Record<KeyTier, string> = { root: "总节点", flash: "子节点" };

/**
 * Key 池面板：两档模型的生效连接 + 连接条目管理（添加即启用、一键切换、
 * 连通性测试）。明文 Key 只在本页表单里出现一次（提交后由后端落 0600
 * 文件），列表与状态永远只显示尾 4 位。切换在下一次扫描启动时生效，
 * 进行中的扫描不受影响。
 */
export function KeyPoolPanel() {
  const [pool, setPool] = useState<KeyPoolPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // 添加表单
  const [tier, setTier] = useState<KeyTier>("root");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [label, setLabel] = useState("");

  const load = useCallback(async () => {
    try {
      setPool(await getKeyPool());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Key 池加载失败");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = useCallback(
    async (body: Parameters<typeof keyPoolAction>[0], busyKey: string) => {
      setBusy(busyKey);
      setNotice(null);
      try {
        const result = await keyPoolAction(body);
        if (result.status) {
          setNotice(
            result.status === "ok"
              ? `连通正常：${result.detail ?? ""}`
              : `测试失败：${result.detail ?? "未知错误"}`
          );
        }
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : "操作失败");
      } finally {
        setBusy(null);
      }
    },
    [load]
  );

  async function submitAdd() {
    if (!model.trim()) {
      setNotice("模型名不能为空");
      return;
    }
    await act(
      {
        action: "add",
        tier,
        model: model.trim(),
        api_key: apiKey.trim() || undefined,
        base_url: baseUrl.trim() || undefined,
        label: label.trim() || undefined,
      },
      "add"
    );
    setModel("");
    setApiKey("");
    setBaseUrl("");
    setLabel("");
  }

  return (
    <div className="keypool-page">
      <div className="kp-nodes">
        {pool &&
          ([pool.root, pool.flash] as const).map((node) => (
            <div className="console-card" key={node.tier}>
              <h2>
                <KeyRound size={16} />
                {TIER_LABELS[node.tier]}当前生效
                {node.source === "keypool" ? (
                  <span className="kp-source is-pool">Key 池</span>
                ) : (
                  <span className="kp-source">.env 默认</span>
                )}
              </h2>
              {node.model ? (
                <>
                  <div className="kp-node-row">
                    <span>厂商</span>
                    <strong>{node.vendor ?? "未知"}</strong>
                  </div>
                  <div className="kp-node-row">
                    <span>模型</span>
                    <strong className="kp-mono">{node.model}</strong>
                  </div>
                  <div className="kp-node-row">
                    <span>API Key</span>
                    <strong className="kp-mono">
                      {node.subscription ? "订阅模式（免 Key）" : node.key_tail ?? "未配置"}
                    </strong>
                  </div>
                  <div className="kp-node-row">
                    <span>端点</span>
                    <strong className="kp-mono">{node.base_url ?? "厂商默认"}</strong>
                  </div>
                  {node.entry_label && (
                    <div className="kp-node-row">
                      <span>条目</span>
                      <strong>{node.entry_label}</strong>
                    </div>
                  )}
                </>
              ) : (
                <p className="field-note">
                  未配置 —— {node.tier === "flash" ? "子 agent 将共用总节点模型" : "开扫前需先配置模型"}
                </p>
              )}
              {node.tier === "root" && (
                <p className="field-note">root agent 决策档；切换在下一次扫描启动时生效。</p>
              )}
            </div>
          ))}
      </div>

      {error && (
        <div className="console-error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="console-error" role="status" style={{ background: "#17362f", borderColor: "#2c5c4f", color: "#a9e8cf" }}>
          {notice}
        </div>
      )}

      <div className="console-card history-card">
        <h2>
          连接条目
          <small>{pool?.entries.length ?? 0} 条</small>
          <button
            className="kp-refresh"
            onClick={() => void load()}
            disabled={busy !== null}
            title="刷新"
          >
            <RefreshCw size={13} />
          </button>
        </h2>
        {pool && pool.entries.length === 0 && (
          <p className="field-note">
            还没有条目。在下方添加一条连接（模型名 + Key），保存后即对该节点生效。
          </p>
        )}
        {pool?.entries.map((entry) => (
          <EntryRow
            key={entry.id}
            entry={entry}
            busy={busy}
            onSwitch={() => void act({ action: "switch", id: entry.id }, `switch:${entry.id}`)}
            onTest={() => void act({ action: "test", id: entry.id }, `test:${entry.id}`)}
            onDelete={() => void act({ action: "delete", id: entry.id }, `delete:${entry.id}`)}
          />
        ))}
      </div>

      <div className="console-card history-card">
        <h2>
          <Plus size={16} />
          添加连接
        </h2>
        <label>
          节点
          <select value={tier} onChange={(e) => setTier(e.target.value as KeyTier)}>
            <option value="root">总节点（root agent 决策档）</option>
            <option value="flash">子节点（子 agent 量产档）</option>
          </select>
        </label>
        <label>
          模型名
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="如 deepseek/deepseek-chat 或裸模型名 kimi-k2.6"
          />
        </label>
        <label>
          API Key
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="sk-…（订阅模式可留空）"
            autoComplete="off"
          />
        </label>
        <label>
          端点 URL<span className="optional">（可选，留空用厂商默认）</span>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.moonshot.cn/v1"
          />
        </label>
        <label>
          备注<span className="optional">（可选）</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="如 deepseek 备用池 A"
          />
        </label>
        <button
          className="start-scan"
          onClick={() => void submitAdd()}
          disabled={busy !== null}
        >
          {busy === "add" ? "保存中…" : "保存并启用"}
        </button>
        <p className="field-note">
          {pool?.pool_path
            ? `存储于 ${pool.pool_path}（0600）。添加即启用；「切换」立即换 active 条目，两者都在下一次扫描启动时生效。`
            : "添加即启用；切换与启用都在下一次扫描启动时生效。"}
        </p>
      </div>
    </div>
  );
}

function EntryRow({
  entry,
  busy,
  onSwitch,
  onTest,
  onDelete,
}: {
  entry: KeyPoolEntry;
  busy: string | null;
  onSwitch: () => void;
  onTest: () => void;
  onDelete: () => void;
}) {
  const testing = busy === `test:${entry.id}`;
  return (
    <div className="kp-entry">
      <span
        className={`kp-status ${entry.last_status ?? "none"}`}
        title={
          entry.last_status === "fail" && entry.last_error
            ? entry.last_error
            : entry.last_checked_at
              ? `上次测试 ${entry.last_checked_at}`
              : "未测试"
        }
      />
      <span className={`kp-tier-badge ${entry.tier}`}>{TIER_LABELS[entry.tier]}</span>
      <div className="kp-entry-main">
        <strong className="kp-mono">{entry.model}</strong>
        <small>
          {entry.vendor ?? "?"} · {entry.key_tail ?? "无 Key"}
          {entry.base_url ? ` · ${entry.base_url}` : ""}
          {entry.label ? ` · ${entry.label}` : ""}
        </small>
        {entry.last_status === "fail" && entry.last_error && (
          <div className="kp-detail">{entry.last_error}</div>
        )}
      </div>
      {entry.active && <span className="kp-active-chip">使用中</span>}
      <div className="kp-actions">
        {!entry.active && (
          <button onClick={onSwitch} disabled={busy !== null}>
            切换
          </button>
        )}
        <button onClick={onTest} disabled={busy !== null}>
          {testing ? "测试中…" : "测试"}
        </button>
        <button className="danger" onClick={onDelete} disabled={busy !== null}>
          <Trash2 size={12} />
        </button>
      </div>
    </div>
  );
}
