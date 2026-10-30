"""Ben.runtime：Docker 运行时。

- ``docker_connection.py``：稳定的 Docker 守护进程连接（复用自 strix）；
- ``docker_executor.py``：执行容器生命周期 + 容器内命令执行；
- ``check_docker_connection.py``：连接自检脚本。
"""

