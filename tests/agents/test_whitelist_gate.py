"""授权白名单闸门的回归测试（Ben 特有硬约束）。

覆盖三类闸门：
  1. 主机白名单：未授权目标拒绝、文件名不误判；
  2. 只读守卫：非只读方法与写负载标志拒绝，读标志（-D/-f/-i/-w/-x）放行；
  3. 间接引用：shell 变量/命令替换拒绝。
"""

from __future__ import annotations

import pytest

from Ben.agents.factory import _check_command_allowed

ALLOWED = ("http://allowed.example:8080",)


@pytest.mark.parametrize(
    "command",
    [
        # 本地文件操作：带扩展名的文件名不得被当成域名
        "python main.py",
        "cat notes.txt",
        "grep -r secret app.py",
        "nmap -oX out.xml target",
        "python3 -m http.server 8000",
        "ls -la",
        "git log --oneline",
        "echo hello",
        # 对白名单目标的正常读取
        "curl http://allowed.example:8080/",
        "curl -sS -D - -o /dev/null http://allowed.example:8080/",
        "curl -f http://allowed.example:8080/",
        "curl -i http://allowed.example:8080/",
        "curl -w %{http_code} http://allowed.example:8080/",
        "curl -x http://127.0.0.1:8080 http://allowed.example:8080/",
        # 回环地址不做白名单校验
        "curl http://127.0.0.1:9999/",
        # 整数 IP 归一化后是回环
        "curl http://2130706433/",
    ],
)
def test_allowed_commands_pass(command: str) -> None:
    assert _check_command_allowed(command, ALLOWED) is None


@pytest.mark.parametrize(
    "command",
    [
        # 未授权目标（含裸域名、userinfo、URL 出现在非 curl 命令里）
        "curl http://evil.com/",
        "nmap evil.com",
        "nc evil.com 80",
        "ping evil.com",
        "echo http://evil.com",
        "ssh user@evil.com",
        "curl -f evil.com",
        "curl -o out.html evil.com",
        # 只读守卫：非只读方法
        "curl -X POST http://allowed.example:8080/",
        "curl --request DELETE http://allowed.example:8080/",
        # 只读守卫：写负载标志（短标志大小写敏感，长标志不敏感）
        "curl -d a=1 http://allowed.example:8080/",
        "curl -F f=@x http://allowed.example:8080/",
        "curl -T payload http://allowed.example:8080/",
        "curl --data a=1 http://allowed.example:8080/",
        "curl --json {} http://allowed.example:8080/",
        # 间接引用：无法静态确定真实目标
        "curl $TARGET/",
        "curl ${TARGET}/",
        "curl $(cat url)",
        "curl `cat url`",
    ],
)
def test_denied_commands_are_rejected(command: str) -> None:
    denial = _check_command_allowed(command, ALLOWED)
    assert denial is not None
    assert denial


def test_hex_ip_form_is_normalized_before_the_whitelist_check() -> None:
    # 0x7f000001 == 127.0.0.1（回环，放行）；若未归一化会被当成陌生域名拒绝
    assert _check_command_allowed("curl http://0x7f000001/", ALLOWED) is None


def test_dump_header_flag_is_not_treated_as_a_payload_flag() -> None:
    # curl -D 是 dump-header（只读），-d 才是 data（写）
    assert _check_command_allowed("curl -D - http://allowed.example:8080/", ALLOWED) is None
    assert _check_command_allowed("curl -d x=1 http://allowed.example:8080/", ALLOWED) is not None


def test_local_dash_d_is_not_a_payload_flag() -> None:
    # cut/tr 的 -d 与 curl 的 --data 无关；管道后的本地命令不得误拒。
    assert (
        _check_command_allowed(
            "curl -sS http://allowed.example:8080/ | cut -d'|' -f1",
            ALLOWED,
        )
        is None
    )
    assert (
        _check_command_allowed(
            "curl -sS -D - http://allowed.example:8080/ | tr -d '\\r'",
            ALLOWED,
        )
        is None
    )
    assert (
        _check_command_allowed(
            "xargs curl -d a=1 http://allowed.example:8080/",
            ALLOWED,
        )
        is not None
    )


def test_filenames_and_attributes_next_to_curl_are_not_hosts() -> None:
    command = (
        "python3 - <<'PY'\n"
        "l=line.lower()\n"
        "print('http' in l)\n"
        "PY\n"
        'echo "===== robots.txt ====="\n'
        "curl -sS https://allowed.example/robots.txt\n"
        'echo "===== sitemap.xml ====="\n'
        "curl -sS -o /tmp/sitemap.xml https://allowed.example/sitemap.xml\n"
        'echo "===== security.txt ====="\n'
    )
    assert _check_command_allowed(command, ALLOWED) is None


def test_python_import_is_not_an_unauthorized_host() -> None:
    command = (
        "python3 - <<'PY'\n"
        "from html.parser import HTMLParser\n"
        "PY\n"
        "curl -sS http://allowed.example:8080/"
    )
    assert _check_command_allowed(command, ALLOWED) is None
