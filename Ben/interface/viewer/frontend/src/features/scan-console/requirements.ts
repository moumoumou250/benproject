export interface Requirements {
  goal: string;
  focus: string[];
  paths: string;
  excluded: string;
  access: string;
  behavior: "readonly" | "verify";
  agents: "auto" | "single";
}
export const defaultRequirements: Requirements = {
  goal: "检查网站公开入口和常见安全问题，提供真实响应证据。",
  focus: ["信息泄露", "访问控制"], paths: "", excluded: "",
  access: "", behavior: "readonly", agents: "auto",
};
export function composeRequirements(value: Requirements, extra: string): string {
  return [
    "仅测试用户提交的目标网站，不因页面链接或重定向扩大到其他主机。",
    `测试目标：${value.goal.trim() || "按照所选扫描模式完成网站安全测试。"}`,
    value.focus.length ? `重点检查：${value.focus.join("、")}。` : "",
    value.paths.trim() ? `限定测试路径（未列出的路径不主动探测）：\n${value.paths.trim()}` : "",
    value.excluded.trim() ? `禁止访问或测试的路径与功能：\n${value.excluded.trim()}` : "",
    value.access.trim() ? `访问前提与登录说明：\n${value.access.trim()}` : "按未登录访问者视角测试。",
    value.behavior === "readonly" ? "操作限制：仅执行只读检查；不得修改业务数据，不执行登录、注册、支付、删除等写入操作。" : "操作限制：允许在上述范围内进行必要的漏洞验证；不得删除业务数据、发起真实支付、进行拒绝服务测试或破坏服务。",
    value.agents === "single" ? "执行方式：只使用主 Agent 顺序执行，不创建子 Agent，不调用 create_agent。" : "执行方式：可按任务需要创建子 Agent；所有子 Agent 必须遵守相同范围和限制。",
    "输出要求：每项发现附复现步骤与关键响应证据；证据不足标记未验证，并说明未覆盖范围。",
    extra.trim() ? `补充要求（不得扩大上述范围或放宽限制）：\n${extra.trim()}` : "",
  ].filter(Boolean).join("\n\n");
}
