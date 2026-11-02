"""把每条 MCP 连接的活跃会话拥有在它自己的监督任务上（对齐 strix 的 mcp/session.py）。

本模块修复的问题：streamable-HTTP 传输（``mcp`` SDK）在 ``server.connect()``
运行时会打开一个内部 anyio 任务组，该任务组的 cancel scope 在调用
``connect()`` 的任务上进入，并在会话整个生命周期内保持打开。旧代码里那个
任务就是运行 agent 循环的主任务。于是当 provider 在传输的某个后台任务上返
回 HTTP 错误（例如后台 POST 收到 403）时，任务组取消它的 scope，取消传播
到主任务，整个扫描以一个裸 ``CancelledError``（被误标为用户中断）挂掉。
随后的拆卸又会抛 "Attempted to exit cancel scope in a different task than
it was entered in"，因为清理运行在与 connect 不同的任务上。

修复方式与子 agent 运行在自己的 ``asyncio.create_task`` 上同理（见
:func:`Ben.core.execution.spawn_child_agent`）：给每条连接一个专属的监督任
务，拥有 ``connect()``、会话的持有期与 ``cleanup()``。三个推论：

- **收容。** 传输的 cancel scope 现在在监督任务上进入，后台失败只取消那个
  任务。运行与其他每条连接继续运转。
- **同任务拆卸。** ``connect()`` 与 ``cleanup()`` 运行在同一任务上，
  "在不同任务退出 cancel scope" 的错误不可能发生。
- **到达调用方的是值，不是取消。** agent 从不直接触碰活跃会话。它把一次调
  用经队列交给监督任务，并把结果作为值 await；会话任务死了，调用方拿到的
  是"连接不可用"的值，而不是传播进 agent 循环的取消。

失败处理遵循连接池纪律：出错即弃，下次使用时重建。连接或重建时的失败描述
的是会话；工具调用收到的非 2xx 响应描述的是那次请求而非会话。调用阶段的
permission 与 protocol 失败返回失败的工具输出，连接保持可用。其他已分类
失败在重建的会话上重试，若持续失败则临时隔离该连接。认证失败与反复的瞬时
失败耗尽会永久退役一条连接。

安全：连接的 :class:`~Ben.tools.mcp.config.McpConnectionConfig` 持有活跃的
bearer 凭据，仅保存在内存中、就在已经持有活跃会话的同一个进程内对象上。
它从不被记日志、从不序列化进运行的事件流、从不落盘；:meth:`__repr__` 省
略它。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import secrets
import time
import weakref
from typing import TYPE_CHECKING, Any, Literal, cast

from Ben.tools.mcp.config import DEFAULT_MAX_CONCURRENT_CALLS
from Ben.tools.mcp.failures import FailureInfo, HttpStatusRecorder, classify


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agents.mcp import MCPServer
    from mcp.types import Tool as MCPTool

    from Ben.tools.mcp.client import ResultTransform
    from Ben.tools.mcp.config import McpConnectionConfig

    # 一次要对活跃会话运行的操作，例如 ``list_tools`` 或一次工具调用。在监督
    # 任务上运行（受监督会话）或内联运行（被收养会话），其返回值成为调用方
    # 的结果。
    Job = Callable[[MCPServer], Awaitable[Any]]

_Phase = Literal["connect", "call"]


logger = logging.getLogger(__name__)

# 优雅（哨兵）关闭等待服务循环排空的时间，超过则改为取消监督任务。给拆卸上
# 界，使缓慢或挂起的在途调用无法无限期卡住它。
_SHUTDOWN_TIMEOUT = 10.0
_MAX_ATTEMPTS = 3
_SETTLE_DELAY = 0.05
_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]] = (
    weakref.WeakKeyDictionary()
)
_JITTER = secrets.SystemRandom()

# SDK 可能为一次失败调用抛出的一切：普通异常加上传输任务组的
# ``BaseExceptionGroup``。整体捕获并交给 ``classify``；``asyncio.CancelledError``
# 总是先单独处理，因此关停与真正的取消仍会传播。
_CLASSIFIABLE: tuple[type[BaseException], ...] = (BaseExceptionGroup, Exception)


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    base = min(8.0, 0.5 * (2 ** (attempt - 1)))
    return base + _JITTER.uniform(0.0, base * 0.1)  # type: ignore[no-any-return]


def _call_semaphore(name: str, limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphores = _SEMAPHORES.setdefault(loop, {})
    return semaphores.setdefault(name, asyncio.Semaphore(limit))


class McpConnectionUnavailableError(RuntimeError):
    """该 MCP 连接此刻无法受理请求。

    由 :meth:`SupervisedMcpSession.list_tools` 在连接已死或处于隔离冷却期时
    抛出。其消息就是会话自己的状态文本，因此派发工具（``describe_mcp``、
    ``call_mcp``）可以原样传给 agent：冷却读起来是暂时的，死连接读起来是终
    局的。:meth:`SupervisedMcpSession.dispatch` 不抛它：对死连接的调用返回
    标准的失败工具输出。
    """


@dataclasses.dataclass
class _Outcome:
    """运行一个 job 的归宿：一个值、一次调用失败，或一条死连接。"""

    value: Any = None
    dead: bool = False
    call_failure: FailureInfo | None = None


@dataclasses.dataclass
class _Request:
    """交给监督任务的一个 job，连同其结果落入的 future。"""

    job: Job
    future: asyncio.Future[_Outcome]
    phase: _Phase


class SupervisedMcpSession:
    """一条 MCP 连接，其活跃会话由专属任务拥有。

    两种构建方式：

    - :meth:`__init__` + :meth:`start` 得到*受监督*会话：引擎拥有连接过程。
      ``start`` 派生监督任务，由它在自己身上构建并连接服务器，然后服务经队
      列交给它的调用。正是这条路径把后台会话失败收容到一个任务里。
    - :meth:`adopt` 得到*被收养*会话：调用方已持有连好的服务器（例如测试
      fake）。没有监督任务；调用针对给定服务器内联运行。仅当提供了 config
      时才可重连。

    派发工具使用的公开异步 API：:meth:`list_tools` 与 :meth:`dispatch`。
    生命周期：:meth:`start`、:meth:`aclose`。只读：:attr:`name`、
    :attr:`server`、:attr:`config`、:attr:`is_dead`。
    """

    def __init__(self, config: McpConnectionConfig) -> None:
        self._name = config.name
        self._config: McpConnectionConfig | None = config
        self._server: MCPServer | None = None
        self._supervised = True
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[_Request | None] | None = None
        self._ready: asyncio.Future[bool] | None = None
        self._pending: set[asyncio.Future[_Outcome]] = set()
        self._dead = False
        self._closing = False
        self._on_dead: Callable[[], None] | None = None
        self._recorder: HttpStatusRecorder | None = None
        self._unavailable_until: float | None = None
        self._quarantine_count = 0
        self._last_failure = FailureInfo("unknown", reason="connection unavailable")
        self._reconnect_lock = asyncio.Lock()
        self._call_semaphore: asyncio.Semaphore | None = None

    @classmethod
    def adopt(
        cls,
        server: MCPServer,
        *,
        name: str,
        config: McpConnectionConfig | None = None,
    ) -> SupervisedMcpSession:
        """把一个已连接的服务器包成无监督任务的会话。

        调用在调用方任务上针对 ``server`` 内联运行，与旧的直接派发行为一致。
        仅当给出 ``config`` 时可重连；否则失败的调用可被隔离但无法复活。
        """
        self = cls.__new__(cls)
        self._name = name
        self._config = config
        self._server = server
        self._supervised = False
        self._task = None
        self._queue = None
        self._ready = None
        self._pending = set()
        self._dead = False
        self._closing = False
        self._on_dead = None
        self._recorder = None
        self._unavailable_until = None
        self._quarantine_count = 0
        self._last_failure = FailureInfo("unknown", reason="connection unavailable")
        self._reconnect_lock = asyncio.Lock()
        self._call_semaphore = None
        return self

    # -- 只读访问器 ----------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def server(self) -> MCPServer | None:
        """当前活跃服务器，连接死后为 ``None``。重连时替换。"""
        return self._server

    @property
    def config(self) -> McpConnectionConfig | None:
        """为重连保留的连接配置。携带 bearer 令牌，绝不要记日志或序列化它。"""
        return self._config

    @property
    def is_dead(self) -> bool:
        return self._dead

    @property
    def is_unavailable(self) -> bool:
        """连接是否处于临时隔离期。"""
        return (
            not self._dead
            and self._unavailable_until is not None
            and time.monotonic() < self._unavailable_until
        )

    def set_on_dead(self, callback: Callable[[], None] | None) -> None:
        """注册一次性回调，在连接转为 dead 时触发。

        回调在把连接标记为 dead 的那个任务上运行（受监督会话是监督任务，被收
        养会话是调用方任务），因此它不能阻塞。它最多触发一次，在健康→死亡
        的边沿；只对干净关闭的连接从不触发。界面层用它推送即时的"离线"状态
        而无需轮询。回调的异常被吞掉（记日志），状态推送永远不能拖垮会话任
        务。
        """
        self._on_dead = callback

    def _mark_dead(self, failure: FailureInfo | None = None, *, attempt: int = 1) -> None:
        """把连接翻转为 dead，并在转变时触发一次 ``on_dead``。"""
        if self._dead:
            return
        failure = failure or self._last_failure
        self._dead = True
        self._unavailable_until = None
        logger.error(
            "MCP connection %r permanently unavailable kind=%s status=%s reason=%s "
            "attempt=%d delay=0",
            self._name,
            failure.kind,
            failure.status,
            failure.reason,
            attempt,
        )
        callback = self._on_dead
        if callback is None:
            return
        try:
            callback()
        except Exception:
            logger.exception("MCP on_dead callback for %r failed", self._name)

    def __repr__(self) -> str:
        # 刻意省略 config，bearer 令牌才不会因意外 repr 此对象而进入日志行。
        return f"SupervisedMcpSession(name={self._name!r}, dead={self._dead})"

    # -- 生命周期 ------------------------------------------------------------

    async def start(self) -> bool:
        """派生监督任务，在它身上连接，并等待就绪。

        会话连上时返回 ``True``；初始连接失败返回 ``False``（调用方随后跳过
        此连接，fail-open）。仅对受监督会话有效。
        """
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._ready = loop.create_future()
        self._task = asyncio.create_task(self._supervise(), name=f"mcp-session-{self._name}")
        return await self._ready

    async def aclose(self) -> None:
        """关停连接，并在其持有任务上清理会话。

        对已连接的受监督会话，这用哨兵向监督任务发信号，使 ``cleanup()`` 运
        行在运行过 ``connect()`` 的同一任务上，是有序关停，监督者能把它与会
        话死亡区分开。拆卸总是有界的：若服务循环不能及时排空哨兵（缓慢或挂
        起的在途调用），或会话从未完成连接（包括在 await 中途被取消的
        connect），则改为取消任务。``_closing`` 先置位，因此监督者把该取消
        当作关停，并仍在自己的任务上清理。
        """
        self._closing = True
        if self._supervised and self._task is not None:
            if not self._task.done():
                # 被取消的就绪 future（connect 在 await 中途被取消）算"未连接"：
                # 绝不对它调 ``.result()``，那会在这里抛出并跳过低下的清理。
                connected = (
                    self._ready is not None
                    and self._ready.done()
                    and not self._ready.cancelled()
                    and self._ready.result()
                )
                if connected and self._queue is not None:
                    # 已到达服务循环：哨兵给出干净、无取消的拆卸，cleanup() 运行
                    # 在监督任务上。但要给它上界：挂起的在途调用会让哨兵永远排在
                    # 它后面，因此排空超时（wait_for 超时即取消）就取消任务。
                    with contextlib.suppress(Exception):
                        await self._queue.put(None)
                    with contextlib.suppress(
                        asyncio.TimeoutError, asyncio.CancelledError, Exception
                    ):
                        await asyncio.wait_for(self._task, _SHUTDOWN_TIMEOUT)
                else:
                    # 还卡在 connect() 里、从未连上、或 connect 被取消：取消以解卡。
                    self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        else:
            await self._safe_cleanup()
        self._fail_pending()

    # -- 面向调用方的操作 -----------------------------------------------------

    async def list_tools(self) -> list[MCPTool]:
        """列出连接的工具，瞬时会话失败会重试。

        连接已死时抛 :class:`McpConnectionUnavailableError`，且永不返回调用失
        败。
        """
        outcome = await self._run_job(lambda server: server.list_tools(), phase="connect")
        if outcome.dead:
            raise McpConnectionUnavailableError(self._unavailable_message())
        if outcome.call_failure is not None:
            raise RuntimeError("MCP list_tools returned a call failure")
        return cast("list[MCPTool]", outcome.value)

    async def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        label: str,
        result_transform: ResultTransform | None = None,
        supports_images: bool = True,
    ) -> Any:
        """运行一次工具调用，瞬时会话失败有有界重试。

        成功时返回工具输出；provider 拒绝调用或连接不可用时返回标准的失败工
        具输出（``success: False``）。调用被拒保持连接可用，因为 provider 拒
        绝的是那次请求，不是会话。
        """
        from Ben.tools.mcp.client import dispatch_mcp_call

        async def job(server: MCPServer) -> Any:
            return await dispatch_mcp_call(
                server,
                tool_name,
                arguments,
                label=label,
                result_transform=result_transform,
                supports_images=supports_images,
            )

        outcome = await self._run_job(job, phase="call")
        if outcome.call_failure is not None:
            from Ben.tools.mcp.client import _errored_tool_output

            return _errored_tool_output(self._call_rejected_message(outcome.call_failure))
        if outcome.dead:
            from Ben.tools.mcp.client import _errored_tool_output

            return _errored_tool_output(self._unavailable_message())
        return outcome.value

    # -- job 路由 -------------------------------------------------------------

    async def _run_job(self, job: Job, *, phase: _Phase) -> _Outcome:
        """把一个 job 路由到持有任务（受监督）或内联运行（被收养）。"""
        if self._supervised:
            return await self._submit(job, phase)
        return await self._execute(job, phase)

    async def _submit(self, job: Job, phase: _Phase) -> _Outcome:
        """把一个 job 交给监督任务，并把它的结果作为值 await。"""
        if self._dead or self._closing or self._task is None or self._task.done():
            return _Outcome(dead=True)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_Outcome] = loop.create_future()
        self._pending.add(future)
        if self._queue is None:
            self._pending.discard(future)
            return _Outcome(dead=True)
        await self._queue.put(_Request(job=job, future=future, phase=phase))
        # 任务可能在上面的检查与 put 之间已结束；那时 ``_fail_pending`` 永远看
        # 不到这个 future，因此在这里直接解决它。
        if self._task.done() and not future.done():
            self._pending.discard(future)
            return _Outcome(dead=True)
        return await future

    # -- 监督任务 -------------------------------------------------------------

    async def _supervise(self) -> None:
        """在一个任务上拥有会话的整个生命：连接、服务、清理。"""
        try:
            self._server = await self._open()
        except asyncio.CancelledError:
            # connect 被取消（运行正在关停，或传输 scope 在 connect 中途取
            # 消）。上报未就绪，让挂接路径把它当作被跳过的连接；不要传播。
            self._report_ready(value=False)
            await self._safe_cleanup()
            self._fail_pending()
            return
        except _CLASSIFIABLE as exc:
            failure = classify(exc)
            logger.warning(
                "Skipping MCP connection %r kind=%s status=%s attempt=1 delay=0",
                self._name,
                failure.kind,
                failure.status,
                exc_info=True,
            )
            self._report_ready(value=False)
            await self._safe_cleanup()
            self._fail_pending()
            return

        self._report_ready(value=True)
        try:
            await self._serve_loop()
        finally:
            await self._safe_cleanup()
            self._fail_pending()

    async def _serve_loop(self) -> None:
        assert self._queue is not None
        while True:
            try:
                request = await self._queue.get()
            except asyncio.CancelledError:
                # 空闲时的取消是传输的任务组因某个后台会话任务失败而取消本监督
                # 任务。在此收容。若正在关闭，这就是普通关停，让它传播。否则隔
                # 离失败的会话并继续服务请求，使稍后的调用能复活它。
                if self._closing:
                    raise
                failure = self._recorder.take() if self._recorder is not None else None
                failure = failure or FailureInfo("transport", reason="session cancelled")
                self._last_failure = failure
                if failure.kind in {"auth", "permission"}:
                    self._mark_dead(failure, attempt=1)
                    return
                await self._quarantine(failure, attempt=1)
                if self._dead:
                    return
                continue
            if request is None:  # 关停哨兵
                return
            outcome = await self._execute(request.job, request.phase)
            if not request.future.done():
                request.future.set_result(outcome)
            self._pending.discard(request.future)
            if self._dead:
                return

    # -- 运行一个 job：有界的分类重试 -----------------------------------------

    async def _execute(self, job: Job, phase: _Phase) -> _Outcome:  # noqa: PLR0912
        """在健康会话上运行一个 job，它一出错就立刻弃置。

        出错即弃、下次使用时重建是这里的全部纪律，它建立在一个不变量上：
        **会话对象只在健康时才被 await。** 调用失败的那一刻，本方法做的下一
        件事——先于任何其他 ``await``，包括 :meth:`_handle_failure` 里的退避
        sleep——就是在这个任务上处置该会话（:meth:`_safe_cleanup` 运行传输拆
        卸并清空 ``_server``）。

        为什么顺序是关键而非讲究：provider 在调用中途返回非 2xx 时，
        streamable-HTTP 传输的任务组取消它的 scope，从而取消本监督任务；失败
        以 ``CancelledError`` 浮现，且该 scope 持续触发（在后续每个
        ``await`` 上重新抛出）直到会话被拆卸。处置会关闭传输的
        AsyncExitStack，从而退出那个触发中的 scope。若先 sleep 退避，sleep
        会重新抛出正在触发的 ``CancelledError``，逃出本方法并杀死监督任务，
        让槽位永远卡在 ``is_dead`` 为 False 的状态。先处置才把失败变成返回
        值，并让任务活着在下次尝试时重建。

        重建本身在循环顶部惰性发生：一旦失败把 ``_server`` 置为 None，下一轮
        迭代构建全新会话（由 :meth:`_reconnect` 守卫）并在其上重试操作。调用
        阶段的 permission 或 protocol 失败在处置后立即返回，因为它描述的是那
        次请求而非会话。真正的关停（``_closing``）与真正的外部取消仍会传
        播；只有传输的拆卸取消被收容。
        """
        if self._dead:
            return _Outcome(dead=True)
        if self._unavailable_until is not None:
            remaining = self._unavailable_until - time.monotonic()
            if remaining > 0:
                return _Outcome(dead=True)
            self._unavailable_until = None
            logger.info(
                "MCP connection %r revive started kind=%s status=%s attempt=1",
                self._name,
                self._last_failure.kind,
                self._last_failure.status,
            )

        if self._call_semaphore is None:
            self._call_semaphore = _call_semaphore(
                self._name,
                (
                    self._config.max_concurrent_calls
                    if self._config is not None
                    else DEFAULT_MAX_CONCURRENT_CALLS
                ),
            )
        failure: FailureInfo | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            # 惰性、原子的重建：此前的失败已处置会话，因此在这里构建新的。重建
            # 锁让并发调用方（从多个 agent 任务派发的被收养会话）共享一次重
            # 建，而不是各建各的。
            if self._server is None:
                reconnected, reconnect_failure = await self._reconnect()
                if not reconnected:
                    failure = reconnect_failure or FailureInfo(
                        "transport", reason="reconnect failed"
                    )
                    outcome = await self._handle_failure(failure, attempt, phase="connect")
                    if outcome is not None:
                        return outcome
                    continue
            assert self._server is not None
            call_semaphore = self._call_semaphore
            assert call_semaphore is not None
            try:
                async with call_semaphore:
                    result = await job(self._server)
                # 成功会清空隔离计次。恢复并服务了一次调用的连接重归健康，因此
                # 被成功复活隔开的瞬时失败脉冲不得累积向永久退役；只有中间没有
                # 成功的持续失败才退役连接。
                self._quarantine_count = 0
                return _Outcome(value=result)
            except asyncio.CancelledError:
                if not self._supervised or self._closing:
                    raise
                failure = (
                    self._recorder.take() if self._recorder is not None else None
                ) or FailureInfo("transport", reason="session cancelled")
                # 先于任何其他 await 处置。传输的 cancel scope 此刻可能正在触
                # 发；_safe_cleanup 退出它，使下面的退避 sleep 无法重新抛出取
                # 消并杀死本任务。为什么这个顺序承重，见方法 docstring。
                await self._safe_cleanup()
            except _CLASSIFIABLE as exc:
                failure = classify(exc)
                if failure.kind == "unknown" and self._recorder is not None:
                    failure = self._recorder.take() or failure
                # 先于任何其他 await 处置，原因同上面的分支：绝不在已出错的会
                # 话上 await。
                await self._safe_cleanup()

            # 会话已处置且 _server 为 None；_handle_failure 可以安全 sleep 退
            # 避，下一轮循环重建并重试。
            outcome = await self._handle_failure(failure, attempt, phase=phase)
            if outcome is not None:
                return outcome
        return _Outcome(dead=True)

    async def _handle_failure(
        self, failure: FailureInfo, attempt: int, *, phase: _Phase
    ) -> _Outcome | None:
        self._last_failure = failure
        if failure.kind == "auth":
            self._mark_dead(failure, attempt=attempt)
            return _Outcome(dead=True)
        if failure.kind == "permission":
            if phase == "call":
                return _Outcome(call_failure=failure)
            self._mark_dead(failure, attempt=attempt)
            return _Outcome(dead=True)
        if phase == "call" and failure.kind == "protocol":
            return _Outcome(call_failure=failure)
        if attempt == _MAX_ATTEMPTS:
            await self._quarantine(failure, attempt=attempt)
            return _Outcome(dead=True)
        delay = _retry_delay(attempt, failure.retry_after)
        self._log_retry(failure, attempt, delay)
        await asyncio.sleep(delay)
        return None

    def _log_retry(self, failure: FailureInfo, attempt: int, delay: float) -> None:
        logger.warning(
            "MCP connection %r retryable failure kind=%s status=%s attempt=%d delay=%.2f",
            self._name,
            failure.kind,
            failure.status,
            attempt,
            delay,
        )

    async def _quarantine(self, failure: FailureInfo, *, attempt: int) -> None:
        await self._safe_cleanup()
        self._quarantine_count += 1
        if self._quarantine_count >= 3:
            self._mark_dead(failure, attempt=attempt)
            return
        cooldown = 30.0 * (2 ** (self._quarantine_count - 1))
        self._unavailable_until = time.monotonic() + cooldown
        logger.warning(
            "MCP connection %r quarantined kind=%s status=%s attempt=%d delay=%.2f",
            self._name,
            failure.kind,
            failure.status,
            attempt,
            cooldown,
        )

    async def _reconnect(self) -> tuple[bool, FailureInfo | None]:
        """在重建锁下构建全新会话，并发调用方共享这一次。

        仅在 ``_server`` 为 None 时调用（此前的失败已处置旧会话）。锁把重建
        串行化；等到锁时发现会话已被先拿到锁的人重建的调用方直接复用它，而
        不是再建一个。这里刻意不清理已有的 ``_server``：本方法从不对着活跃会
        话运行，因为失败路径在到达重建之前就已处置。
        """
        async with self._reconnect_lock:
            if self._server is not None:
                # 等锁期间另一个调用方已重建；共享它。
                return True, None
            if self._config is None:
                return False, FailureInfo("transport", reason="no reconnect config")
            try:
                server = await self._open()
            except asyncio.CancelledError:
                if self._closing:
                    raise
                self._server = None
                return False, FailureInfo("transport", reason="reconnect cancelled")
            except _CLASSIFIABLE as exc:
                self._server = None
                failure = classify(exc)
                if failure.kind == "unknown" and self._recorder is not None:
                    failure = self._recorder.take() or failure
                return False, failure
            # connect() 是 SDK 暴露的唯一就绪面。
            self._server = server
            try:
                await asyncio.sleep(_SETTLE_DELAY)
            except asyncio.CancelledError:
                # 返回前处置刚建好的会话；_safe_cleanup 在关停时会重新抛出，其
                # 他情况下吸收。
                await self._safe_cleanup()
                if self._closing:
                    raise
                return False, FailureInfo("transport", reason="reconnect cancelled")
            return True, None

    async def _open(self) -> MCPServer:
        """构建并连接 SDK 服务器，复用既有的搭建步骤。

        若 ``connect()`` 失败，刚建好的服务器在错误传播之前就在这里、在同一
        任务上被清理，因此失败的 connect 永远不会留下孤儿 MCP 子进程或半开的
        HTTP 会话。
        """
        from Ben.tools.mcp.client import _build_server

        if self._config is None:
            raise RuntimeError(f"MCP connection {self._name!r} has no config to connect")
        built = _build_server(self._config)
        server = built.server
        self._recorder = built.recorder
        try:
            await server.connect()  # type: ignore[no-untyped-call]
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await server.cleanup()  # type: ignore[no-untyped-call]
            raise
        except _CLASSIFIABLE:
            with contextlib.suppress(Exception):
                await server.cleanup()  # type: ignore[no-untyped-call]
            raise
        return server

    # -- 辅助 -----------------------------------------------------------------

    def _call_rejected_message(self, failure: FailureInfo) -> str:
        if failure.kind == "permission":
            return (
                f"MCP connection {self._name!r} rejected this call (status={failure.status}): "
                "the provider denied this specific request, not the connection. The connection "
                "is still available. Check the arguments — resource and project identifiers, "
                "and required fields — and whether the configured credential is allowed to read "
                "that resource, then retry."
            )
        if failure.kind == "protocol":
            if failure.status is None:
                return (
                    f"MCP connection {self._name!r} rejected this call: the provider "
                    "returned an error for this request, not the connection. The connection "
                    "is still available. The resource may not exist or the arguments may be "
                    "wrong. Check them with describe_mcp, then retry or move on."
                )
            return (
                f"MCP connection {self._name!r} rejected this call as invalid "
                f"(status={failure.status}): the request itself was malformed, not the "
                "connection. The connection is still available. Check the tool's required "
                "arguments and value formats with describe_mcp, then retry."
            )
        raise AssertionError(f"Unexpected call failure kind: {failure.kind}")

    async def _safe_cleanup(self) -> None:
        """在本任务上处置活跃会话，即使 cancel scope 正在触发也完成拆卸。

        棘手之处：streamable-HTTP 传输持有一个 anyio 任务组，其 cancel scope
        是在本监督任务上进入的。当后台 POST 收到非 2xx 时 SDK 取消了那个
        scope，而在 scope 退出之前，本任务上的每个 ``await`` 都会重抛
        ``CancelledError``。``server.cleanup()`` 关闭运行任务组
        ``__aexit__`` 的 AsyncExitStack，而那个 ``__aexit__`` 正是退出 scope、
        停止触发的东西；它还在内部吸收 scope 自身的取消，因此常见情形干净返
        回。不过仍可能有零星的 ``CancelledError`` 冒出来，而
        ``contextlib.suppress(Exception)`` 会放它过去，因为
        ``CancelledError`` 是 ``BaseException`` 而非 ``Exception``。

        所以显式捕获 ``CancelledError``。真正的关停（``_closing``）中，该取
        消是运行在关停，必须传播，因此重新抛出。否则吸收它并有界地重试关
        闭：若一次清理在退出栈展开完之前被打断，再关一次会从断点继续（栈一
        次弹一个回调），scope 最终仍被退出，本任务保持可运行以迎接下次重
        建。
        """
        server = self._server
        self._server = None
        if server is None:
            return
        for _ in range(_MAX_ATTEMPTS):
            try:
                # suppress(Exception) 吸收普通清理错误但放 CancelledError 过去
                # （它是 BaseException）；由下面的外层处理器决定传播还是重试。
                with contextlib.suppress(Exception):
                    await server.cleanup()  # type: ignore[no-untyped-call]
            except asyncio.CancelledError:
                if self._closing:
                    raise
                # 触发中的 scope 在栈展开完之前击中了 cleanup 的 await；吞掉此
                # 取消并再次关闭以完成拆卸。完全关闭的栈使重试成为干净的空操
                # 作。
                continue
            else:
                return

    def _report_ready(self, value: bool) -> None:
        if self._ready is not None and not self._ready.done():
            self._ready.set_result(value)

    def _fail_pending(self) -> None:
        for future in self._pending:
            if not future.done():
                future.set_result(_Outcome(dead=True))
        self._pending.clear()

    def _unavailable_message(self) -> str:
        if self._unavailable_until is not None:
            remaining = max(0.0, self._unavailable_until - time.monotonic())
            return (
                f"MCP connection {self._name!r} is temporarily unavailable "
                f"(kind={self._last_failure.kind}, status={self._last_failure.status}); "
                f"retrying in about {remaining:.0f} seconds."
            )
        return (
            f"MCP connection {self._name!r} is unavailable "
            f"(kind={self._last_failure.kind}, status={self._last_failure.status}); "
            "it will not be retried."
        )
