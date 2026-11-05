import type { Requirements } from "../requirements";

const focuses = ["信息泄露", "访问控制", "身份认证", "输入验证", "API 安全", "安全响应头", "业务逻辑"];
export function TestRequirements({ value, onChange, disabled }: { value: Requirements; onChange: (value: Requirements) => void; disabled: boolean }) {
  function update<K extends keyof Requirements>(key: K, next: Requirements[K]) { onChange({ ...value, [key]: next }); }
  return <fieldset className="test-requirements" disabled={disabled}><legend>测试要求</legend>
    <p className="requirements-note">填写后自动生成测试指令。适用于当前网站目标，无需手写命令。</p>
    <label>希望验证什么<textarea rows={2} maxLength={1500} value={value.goal} onChange={e => update("goal", e.target.value)} placeholder="例如：检查公开 API 是否暴露敏感信息"/></label>
    <div className="focus-options"><span>重点检查项</span><div>{focuses.map(focus => <label key={focus}><input type="checkbox" checked={value.focus.includes(focus)} onChange={e => update("focus", e.target.checked ? [...value.focus, focus] : value.focus.filter(item => item !== focus))}/>{focus}</label>)}</div></div>
    <div className="requirements-columns"><label>限定路径 · 可选<textarea rows={3} maxLength={1500} value={value.paths} onChange={e => update("paths", e.target.value)} placeholder={'/api/products\n/rest/user（每行一项）'}/></label><label>排除路径或功能 · 可选<textarea rows={3} maxLength={1500} value={value.excluded} onChange={e => update("excluded", e.target.value)} placeholder={'/logout\n支付、删除账号'}/></label></div>
    <label>操作范围<select value={value.behavior} onChange={e => update("behavior", e.target.value as Requirements["behavior"])}><option value="readonly">只读检查，不修改业务数据</option><option value="verify">允许范围内的必要漏洞验证</option></select></label>
    <label>Agent 执行方式<select value={value.agents} onChange={e => update("agents", e.target.value as Requirements["agents"])}><option value="auto">自动分工，可创建子 Agent</option><option value="single">单 Agent 顺序执行</option></select></label>
    <label>访问前提 / 登录流程 · 可选<textarea rows={2} maxLength={1500} value={value.access} onChange={e => update("access", e.target.value)} placeholder="描述所需角色、登录入口或已配置的测试会话；留空按未登录视角测试。"/></label>
    <p className="requirements-note">这些选项会作为 Agent 测试指令传入，不等同于网络层强制隔离。只读模式不执行登录等写入操作。</p>
  </fieldset>;
}
