"""知识片段（skill）加载机制，机制与结构对齐 strix/skills/__init__.py。

布局（与 strix 相同）：加载器与片段数据同居本包目录——
``Ben/skills/<category>/<name>.md``（等价 strix 的 ``strix/skills/``）。
外部目录经 :func:`register_skill_dir` 注册，**后注册者优先**，可覆盖同名
片段（同名指 ``category/name`` 限定名相同）。

命名（与 strix 相同）：
- 限定名 ``category/name``（如 ``scan_modes/blackbox``）精确命中；
- 裸名 ``name`` 跨分类解析，命中多个分类时报歧义，需改用限定名；
- 片段根下的散置 .md 归入 ``root`` 分类；``README.md`` 与 ``__`` 开头文件跳过。

frontmatter：YAML，至少 ``name``、``description``，可选 ``template: jinja``
（该标记的片段返回前先经 Jinja 渲染，环境同 strix：StrictUndefined +
trim_blocks/lstrip_blocks/keep_trailing_newline，autoescape 关闭）。

安全约束：所有读取限制在已注册片段目录内，按名查表、不接受路径；
纯只读，不产生任何写/执行能力。与 strix 的有意差异：按名加载失败抛
明确异常（strix 仅告警跳过），以满足"渲染失败明确报错"的 Ben 约束。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping

import yaml

logger = logging.getLogger(__name__)

# 内置片段根：本包目录（加载器与数据同居，对齐 strix/skills/ 布局）。
DEFAULT_SKILL_DIR = Path(__file__).resolve().parent

# 片段名/分类名规范（裸名；限定名 = 两段以 / 相连）。
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# frontmatter：文件开头的 --- ... --- 块。
_FRONTMATTER_RE = re.compile(r"\A[ \t]*---[ \t]*\r?\n(.*?)\r?\n?[ \t]*---[ \t]*\r?\n?", re.DOTALL)

# template 标记取值：jinja -> 先渲染再返回；none/缺省 -> 原样返回。
_TEMPLATE_JINJA = "jinja"
# 片段根下散置文件的分类名（对齐 strix 的 root 分类）。
_ROOT_CATEGORY = "root"
# 跳过的文件名（对齐 strix：README 与 __ 前缀不参与发现）。
_SKIP_FILES = {"README.md"}


class SkillError(RuntimeError):
    """片段机制错误基类。"""


class SkillNotFoundError(SkillError):
    """按名加载了不存在的片段。"""


class SkillAmbiguousError(SkillError):
    """裸名命中多个分类，需改用 category/name 限定名。"""


class SkillRenderError(SkillError):
    """template: jinja 片段渲染失败（明确报错，不静默返回空）。"""


@dataclass(frozen=True)
class Skill:
    """一个已解析的片段：frontmatter 元数据 + 剥除 frontmatter 后的正文。"""

    name: str  # 裸名（frontmatter name）
    description: str
    category: str  # 分类名 = 所在子目录名（片段根散置文件归 root）
    is_template: bool  # frontmatter template == "jinja"
    body: str  # 剥除 frontmatter 后的正文
    source: Path  # 来源文件（用于告警与覆盖追踪）

    @property
    def qualified_name(self) -> str:
        """category/name 限定名。"""
        return f"{self.category}/{self.name}"


# 搜索目录列表：索引 0 为内置目录；register_skill_dir 追加到尾部，
# 发现时“后注册者优先”（尾部目录的同名片段覆盖前部）。
_search_dirs: list[Path] = [DEFAULT_SKILL_DIR]
# 发现结果缓存（键为 category/name 限定名）；register_skill_dir 后失效重建。
_skill_cache: dict[str, Skill] | None = None


def register_skill_dir(path: str | Path) -> Path:
    """注册外部片段目录（后注册者优先，可覆盖同名片段）。

    目录布局须与内置一致：``<root>/<category>/<name>.md``。
    重复注册同一目录幂等；目录不存在时记录告警并跳过（不中断加载）。
    """
    global _skill_cache
    dir_path = Path(path).resolve()
    if dir_path in _search_dirs:
        return dir_path
    if not dir_path.is_dir():
        logger.warning("注册片段目录不存在，已跳过：%s", dir_path)
        return dir_path
    _search_dirs.append(dir_path)
    _skill_cache = None  # 使缓存失效，下一次访问重新发现
    logger.info("已注册片段目录：%s（同名片段以此目录为准）", dir_path)
    return dir_path


def skill_search_dirs() -> tuple[Path, ...]:
    """返回当前生效的片段搜索目录（含外部注册，按优先级从低到高）。"""
    return tuple(d for d in _search_dirs if d.is_dir())


def _parse_skill_file(path: Path, category: str) -> Skill | None:
    """解析单个 .md 片段文件；frontmatter 非法时告警并返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("片段文件读取失败，已跳过：%s（%s）", path, exc)
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        logger.warning("片段缺少 YAML frontmatter，已跳过：%s", path)
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        logger.warning("片段 frontmatter YAML 非法，已跳过：%s（%s）", path, exc)
        return None
    if not isinstance(meta, dict):
        logger.warning("片段 frontmatter 必须是键值映射，已跳过：%s", path)
        return None

    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        logger.warning("片段 frontmatter 缺少 name/description，已跳过：%s", path)
        return None
    if not _NAME_RE.match(name):
        logger.warning("片段名 %r 不符合命名规范 %s，已跳过：%s", name, _NAME_RE.pattern, path)
        return None

    template_flag = str(meta.get("template") or "none").strip().lower()
    if template_flag not in ("none", _TEMPLATE_JINJA):
        logger.warning("片段 %s 的 template 标记 %r 无法识别，按 none 处理：%s",
                       name, template_flag, path)
        template_flag = "none"

    return Skill(
        name=name,
        description=description,
        category=category,
        is_template=template_flag == _TEMPLATE_JINJA,
        body=text[match.end():].strip("\n"),
        source=path,
    )


def _iter_skill_files(dir_path: Path):
    """产出 (category, 文件路径)：子目录名为分类；根目录散置文件归 root。"""
    for md_path in sorted(dir_path.rglob("*.md")):
        if md_path.name in _SKIP_FILES or md_path.name.startswith("__"):
            continue
        category = (
            _ROOT_CATEGORY if md_path.parent == dir_path else md_path.parent.name
        )
        yield category, md_path


def _discover() -> dict[str, Skill]:
    """扫描全部搜索目录，构建 {category/name: Skill} 表。

    冲突处理：同一目录内同名 -> 告警歧义、按文件名序取前者；跨目录同名 ->
    后注册目录覆盖（设计行为，debug 日志记录）。
    """
    global _skill_cache
    if _skill_cache is not None:
        return _skill_cache

    skills: dict[str, Skill] = {}
    for dir_path in _search_dirs:
        if not dir_path.is_dir():
            continue
        local: dict[str, Skill] = {}
        for category, md_path in _iter_skill_files(dir_path):
            skill = _parse_skill_file(md_path, category)
            if skill is None:
                continue
            if skill.qualified_name in local:
                logger.warning(
                    "目录 %s 内片段 %r 歧义（%s 与 %s），按文件名序保留前者",
                    dir_path, skill.qualified_name,
                    local[skill.qualified_name].source, skill.source,
                )
                continue
            local[skill.qualified_name] = skill
        for qname, skill in local.items():
            if qname in skills:
                logger.debug("片段 %r 被后注册目录覆盖：%s -> %s",
                             qname, skills[qname].source, skill.source)
            skills[qname] = skill

    _skill_cache = skills
    return skills


def list_skills() -> dict[str, Skill]:
    """返回全部已发现片段（{category/name: Skill}）。"""
    return dict(_discover())


# system prompt 里 <available_skills> 只需要索引层；正文经 load_skill 按需拉。
_CATALOG_DESCRIPTION_MAX = 80


def _catalog_description(text: str, *, limit: int = _CATALOG_DESCRIPTION_MAX) -> str:
    """把目录 description 裁到短索引句（超出加省略号）。"""
    desc = (text or "").strip()
    if len(desc) <= limit:
        return desc
    # 预留 "..."；尽量在空白处切断，避免半截词。
    cut = desc[: max(1, limit - 3)].rstrip()
    space = cut.rfind(" ")
    if space >= max(16, (limit - 3) // 2):
        cut = cut[:space].rstrip()
    return cut + "..."


def skill_catalog() -> dict[str, list[dict[str, str]]]:
    """生成分类目录：{分类: [{name, description}, ...]}（只含元数据，不含正文）。

    description 截到约 80 字符，避免目录块占满 system prompt 前缀。
    """
    catalog: dict[str, list[dict[str, str]]] = {}
    for skill in sorted(_discover().values(),
                        key=lambda s: (s.category, s.name)):
        catalog.setdefault(skill.category, []).append(
            {
                "name": skill.name,
                "description": _catalog_description(skill.description),
            }
        )
    return catalog


def get_available_skills() -> dict[str, list[dict[str, str]]]:
    """strix 兼容别名（report/coverage 等 strix 移植代码使用）：等价 :func:`skill_catalog`。"""
    return skill_catalog()


def resolve_skill_name(name: str) -> str:
    """把裸名/限定名解析为限定名；未命中或歧义时抛明确异常。"""
    key = (name or "").strip()
    skills = _discover()
    if "/" in key:
        parts = key.split("/")
        if len(parts) != 2 or not all(_NAME_RE.match(p) for p in parts):
            logger.warning("请求了非法片段名：%r", name)
            raise SkillNotFoundError(
                f"非法片段名：{name!r}（限定名应为 category/name）"
            )
        if key not in skills:
            logger.warning("请求了未注册片段：%s", key)
            raise SkillNotFoundError(
                f"未注册片段：{key}（可用：{sorted(skills)}）"
            )
        return key
    if not _NAME_RE.match(key):
        logger.warning("请求了非法片段名：%r", name)
        raise SkillNotFoundError(f"非法片段名：{name!r}（规范：{_NAME_RE.pattern}）")
    matches = [q for q, s in skills.items() if s.name == key]
    if not matches:
        logger.warning("请求了未注册片段：%s", key)
        raise SkillNotFoundError(
            f"未注册片段：{key}（可用：{sorted(s.name for s in skills.values())}）"
        )
    if len(matches) > 1:
        logger.warning("片段裸名 %r 歧义：%s", key, matches)
        raise SkillAmbiguousError(
            f"片段名 {key!r} 命中多个分类，请使用限定名：{sorted(matches)}"
        )
    return matches[0]


def get_skill(name: str, *, context: Mapping[str, Any] | None = None) -> str:
    """按名（裸名或 category/name）加载片段正文。

    ``template: jinja`` 片段先用 context 渲染再返回。缺失/歧义/渲染失败
    均抛明确异常（不静默返回空串）。
    """
    skill = _discover()[resolve_skill_name(name)]
    if not skill.is_template:
        return skill.body
    return _render_skill(skill, context or {})


def validate_requested_skills(
    skill_list: list[str], max_skills: int = 5
) -> str | None:
    """校验请求片段清单（对齐 strix 的 validate_requested_skills）。

    返回 None 表示通过；否则返回可供模型阅读的错误描述（数量超限、
    未知名称、裸名歧义）。
    """
    if len(skill_list) > max_skills:
        return (
            f"最多指定 {max_skills} 个片段；收到 {len(skill_list)} 个。"
            "请精简为 1-3 个相关片段。"
        )
    errors: list[str] = []
    for name in skill_list:
        try:
            resolve_skill_name(name)
        except SkillError as exc:
            errors.append(str(exc))
    return "\n".join(errors) if errors else None


def _render_skill(skill: Skill, context: Mapping[str, Any]) -> str:
    """用独立 Jinja 环境渲染模板片段（配置对齐 strix 的 _SKILL_TEMPLATES）。"""
    from jinja2 import BaseLoader, Environment, StrictUndefined

    env = Environment(
        loader=BaseLoader(),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        keep_trailing_newline=True,
    )
    try:
        return env.from_string(skill.body).render(dict(context))
    except Exception as exc:  # noqa: BLE001 - 统一转为明确的片段渲染错误
        raise SkillRenderError(
            f"片段 {skill.qualified_name}（{skill.source}）渲染失败："
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _candidate_qualified_names(name: str) -> set[str]:
    """把技能名解析为候选限定名集合（对齐 strix 的 _candidate_skill_files）。

    限定名 ``category/name`` 精确命中；裸名跨分类匹配所有同名技能。
    未注册的名字解析为空集合。
    """
    skills = _discover()
    if "/" in name:
        return {name} if name in skills else set()
    return {qname for qname, skill in skills.items() if skill.name == name}


def skill_turn_limit(skill_names: list[str], turn_limits: Mapping[str, int]) -> int | None:
    """``turn_limits``（按技能名索引）中命中这些技能的最大值，无命中返回 None。

    对齐 strix 的 skill_turn_limit：子 agent 挂载了带轮数上限的技能时，
    用其上限收紧执行循环。
    """
    files = {q for name in skill_names for q in _candidate_qualified_names(name)}
    return max(
        (
            limit
            for name, limit in turn_limits.items()
            if files.intersection(_candidate_qualified_names(name))
        ),
        default=None,
    )


__all__ = [
    "DEFAULT_SKILL_DIR",
    "Skill",
    "SkillAmbiguousError",
    "SkillError",
    "SkillNotFoundError",
    "SkillRenderError",
    "get_available_skills",
    "get_skill",
    "list_skills",
    "register_skill_dir",
    "resolve_skill_name",
    "skill_catalog",
    "skill_search_dirs",
    "skill_turn_limit",
    "validate_requested_skills",
]
