# agent-browser

Browser automation CLI installed in the sandbox image. Driven by the agent
through `exec_command` (not a function tool) — 与 strix 相同的架构。

- **Implementation:** sandbox CLI at
  `/home/pentester/.npm-global/bin/agent-browser` — npm package
  `agent-browser@0.26.0` (Vercel), driving Chromium directly via CDP.
  Ben 的 `executor_image`（`ghcr.io/usestrix/strix-sandbox`）已预装，
  无需额外安装。
- **Skill:** `Ben/skills/tooling/agent_browser.md`（自 strix 复制适配），
  通过 `load_skill` 按需加载（jinja 渲染，`supports_images=False`）。
- **Proxy:** 容器 ENTRYPOINT 启动 Caido 后，浏览器 HTTP(S) 流量经
  `http_proxy`/`https_proxy` 自动留痕（CA 证书已被 entrypoint 加入
  Chromium NSS 信任库）；localhost CDP 控制流量由 `NO_PROXY` 豁免。
- **验证:** 已在沙箱内实测 `agent-browser open/snapshot/screenshot/close`
  全流程可用。
