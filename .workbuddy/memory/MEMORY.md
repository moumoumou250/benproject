# BenProject 长期备忘

- 本机（Linux）全量 pytest 有约 99 个预先存在的顺序依赖/环境失败（test_workspace_files 等，Windows-first 代码库）；单文件跑全过。改动的回归判定：单文件/相关子集通过即可，全量失败数以 99 为基线比较。
- 全量回归跑"干净基线"时必须 `--ignore=tests/test_model_tiering.py` 之类的排除：stash 不含未跟踪文件，未跟踪测试会在干净树上收集报错。
- 模型分层：root 用 LLM_MODEL（旗舰档），子 agent 用 BEN_FLASH_MODEL（flash 档）；带前缀模型 key 走厂商专属变量（DEEPSEEK_API_KEY 等），裸模型名走 BEN_FLASH_API_KEY + BEN_FLASH_API_BASE。机制 = Agent.model 非 None 时覆盖 RunConfig.model（openai-agents SDK 规则）。
