"""外部片段目录（``register_skill_dir``）的发现、覆盖与安全约束。

对应 strix 的 ``tests/test_skill_dir_extension.py``，但按 Ben 的 loader API 重写：
上游用的是 ``get_all_skill_names``/``load_skills`` 等函数，Ben 的 ``Ben/skills``
是可读性重写版（明确的 ``SkillNotFoundError``/``SkillRenderError``），
接口为 ``list_skills()`` / ``resolve_skill_name()`` / ``get_skill()``。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import Ben.skills as skills_mod
from Ben.skills import (
    SkillNotFoundError,
    get_skill,
    list_skills,
    register_skill_dir,
    resolve_skill_name,
)


@pytest.fixture(autouse=True)
def _isolate_skill_registry() -> Iterator[None]:
    """每个用例前后复原搜索目录与发现缓存，避免互相污染。"""
    original_dirs = list(skills_mod._search_dirs)
    original_cache = skills_mod._skill_cache
    try:
        yield
    finally:
        skills_mod._search_dirs[:] = original_dirs
        skills_mod._skill_cache = original_cache


def _write_skill(root: Path, category: str, name: str, *, description: str = "desc") -> Path:
    category_dir = root / category
    category_dir.mkdir(parents=True, exist_ok=True)
    path = category_dir / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n正文 {name}\n",
        encoding="utf-8",
    )
    return path


def test_builtin_skills_are_discovered() -> None:
    skills = list_skills()
    assert {"tooling/nmap", "vulnerabilities/ssrf"}.issubset(set(skills))
    # 本次移植新增的两类知识包
    assert {"frameworks/django", "cloud/kubernetes"}.issubset(set(skills))


def test_register_is_idempotent(tmp_path: Path) -> None:
    register_skill_dir(tmp_path)
    register_skill_dir(tmp_path)
    assert list(skills_mod.skill_search_dirs()).count(tmp_path.resolve()) == 1


def test_missing_directory_is_skipped_silently(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    register_skill_dir(missing)
    assert missing.resolve() not in skills_mod.skill_search_dirs()


def test_registered_skill_becomes_loadable(tmp_path: Path) -> None:
    _write_skill(tmp_path, "custom_team", "house_rules", description="团队内部规则")
    register_skill_dir(tmp_path)

    skills = list_skills()
    assert "custom_team/house_rules" in skills
    assert "正文 house_rules" in get_skill("custom_team/house_rules")


def test_later_registered_dir_shadows_earlier(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_skill(first, "custom_team", "house_rules", description="第一版")
    _write_skill(second, "custom_team", "house_rules", description="第二版")

    register_skill_dir(first)
    register_skill_dir(second)

    assert list_skills()["custom_team/house_rules"].description == "第二版"


def test_fragment_without_frontmatter_is_skipped(tmp_path: Path) -> None:
    category_dir = tmp_path / "custom_team"
    category_dir.mkdir(parents=True)
    (category_dir / "bare.md").write_text("没有 frontmatter\n", encoding="utf-8")
    register_skill_dir(tmp_path)

    assert "custom_team/bare" not in list_skills()


def test_fragment_missing_description_is_skipped(tmp_path: Path) -> None:
    category_dir = tmp_path / "custom_team"
    category_dir.mkdir(parents=True)
    (category_dir / "half.md").write_text("---\nname: half\n---\n正文\n", encoding="utf-8")
    register_skill_dir(tmp_path)

    assert "custom_team/half" not in list_skills()


@pytest.mark.parametrize("name", ["../../etc/passwd", "a/b/c", "", "  "])
def test_rejects_malformed_or_path_like_names(name: str) -> None:
    with pytest.raises(SkillNotFoundError):
        resolve_skill_name(name)


def test_unknown_skill_name_raises_instead_of_silently_returning() -> None:
    with pytest.raises(SkillNotFoundError):
        resolve_skill_name("custom_team/does_not_exist")
