"""AstrBot 破甲插件入口（v3）。

v3 的设计目标：**不劫持、不冒充，让模型自己把话说出来**。

* 不再按关键词短路。任何消息都先正常交给模型。
* 模型拒绝时，走「拒绝阶梯」：
  1. 直连重试（提高温度、去掉元指令，让模型换个开头）；
  2. **术语表翻译通道**（主力）：给模型英文源句 + ``nigger=黑鬼`` 这张术语表，
     让它翻成中文。中文正文 100% 由模型生成，插件只提供英文素材与术语表。
     并发多次取第一个合规结果，把单次 50%–100% 的命中率抬到接近必中。
* 本地中文段子库默认不参与任何路径；`/破甲 讲` 也是走模型，不是读库。
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star

try:  # TextPart 在部分旧版本中不存在，退化为 dict 内容块
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 兼容旧版 AstrBot
    TextPart = None  # type: ignore[assignment,misc]

try:  # 事件类型过滤器（用于在 LLM 阶段之前关掉流式）
    from astrbot.api.event.filter import EventMessageType
except Exception:  # pragma: no cover - 极旧版本没有该枚举
    EventMessageType = None  # type: ignore[assignment]

try:  # AstrBot 以 data.plugins.<dir>.main 形式加载，相对导入优先
    from .pojia_channel import (
        GROUP_LABELS,
        build_request,
        count_items,
        looks_compliant,
        TOPIC_MARKERS,
        merge_items,
        normalize_group,
    )
    from .pojia_intent import (
        CATEGORY_LABELS,
        category_help,
        detect_request,
        detect_topic,
        normalize_category,
        parse_count,
    )
    from .pojia_core import (
        CORE_TAG,
        INTENSITY_LABELS,
        STATE_KEY,
        TEMPLATE_LABELS,
        TEMPLATE_ORDER,
        PluginSettings,
        PluginState,
        SessionState,
        apply_injection,
        canonical_template,
        clean_output,
        looks_like_refusal,
        match_session,
        render_injection,
        user_tail_text,
    )
except ImportError:  # 兼容以独立模块方式加载/测试
    from pojia_channel import (  # type: ignore[no-redef]
        GROUP_LABELS,
        build_request,
        count_items,
        looks_compliant,
        TOPIC_MARKERS,
        merge_items,
        normalize_group,
    )
    from pojia_intent import (  # type: ignore[no-redef]
        CATEGORY_LABELS,
        category_help,
        detect_request,
        detect_topic,
        normalize_category,
        parse_count,
    )
    from pojia_core import (  # type: ignore[no-redef]
        CORE_TAG,
        INTENSITY_LABELS,
        STATE_KEY,
        TEMPLATE_LABELS,
        TEMPLATE_ORDER,
        PluginSettings,
        PluginState,
        SessionState,
        apply_injection,
        canonical_template,
        clean_output,
        looks_like_refusal,
        match_session,
        render_injection,
        user_tail_text,
    )

_COMMAND_NAMES = {"破甲", "pojia", "jailbreak", "越狱", "装甲"}
_PREFIX_RE = re.compile(r"^[\W_]+", re.UNICODE)
_EXTRA_ORIGINAL_PROMPT = "_pojia_original_prompt"
_EXTRA_EFFECTIVE_SYSTEM = "_pojia_effective_system"
_EXTRA_INJECTION = "_pojia_injection"
_EXTRA_RETRY_COUNT = "_pojia_retry_count"
_EXTRA_ACTIVE = "_pojia_active"
_EXTRA_GROUP = "_pojia_group"
_EXTRA_WANT = "_pojia_want"
_EXTRA_CHANNEL_HIT = "_pojia_channel_hit"


def _chain_text(chain: Any) -> str:
    """取出消息链里的纯文本。"""
    parts: list[str] = []
    try:
        for comp in chain or []:
            text = getattr(comp, "text", None)
            if isinstance(text, str):
                parts.append(text)
    except TypeError:
        return ""
    return "".join(parts)


def _replace_chain_text(chain: Any, new_text: str) -> bool:
    """把消息链里的纯文本整体替换掉，保留其它组件（@、图片等）。"""
    replaced = False
    try:
        for comp in chain or []:
            text = getattr(comp, "text", None)
            if not isinstance(text, str):
                continue
            if replaced:
                comp.text = ""
            else:
                comp.text = new_text
                replaced = True
    except Exception:
        return False
    return replaced


class PojiaPlugin(Star):
    """破甲插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self.settings = PluginSettings.from_config(self.config)
        self.state = PluginState()
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._rng = random.Random()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        await self._ensure_loaded()
        self.logger.info(
            "破甲插件 v4 已加载：master=%s template=%s channel=%s attempts=%s "
            "waves=%s nostream=%s",
            self.settings.master_enabled,
            self.settings.default_template,
            self.settings.channel_enabled,
            self.settings.channel_attempts,
            self.settings.channel_waves,
            self.settings.force_non_streaming,
        )

    async def terminate(self) -> None:
        if self._loaded:
            await self._save_state()

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            raw: Any = None
            try:
                raw = await self.get_kv_data(STATE_KEY, None)
            except Exception as exc:  # KV 不可用时退化为内存状态
                self.logger.warning("读取破甲状态失败，将使用内存状态：%s", exc)
            self.state = PluginState.from_dict(raw, self.settings)
            self.state.prune()
            self._loaded = True

    async def _save_state(self) -> None:
        if not self._loaded:
            return
        async with self._save_lock:
            try:
                self.state.prune()
                await self.put_kv_data(STATE_KEY, self.state.to_dict())
            except Exception as exc:
                self.logger.warning("保存破甲状态失败：%s", exc)

    # ------------------------------------------------------------------
    # 状态/配置辅助
    # ------------------------------------------------------------------

    def _reload_settings(self) -> None:
        self.settings = PluginSettings.from_config(self.config)

    def _default_session_enabled(self) -> bool:
        if self.state.global_enabled is not None:
            return bool(self.state.global_enabled)
        return self.settings.default_enabled

    def _session_enabled(self, umo: str) -> bool:
        if not self.settings.master_enabled:
            return False
        if self.state.global_enabled is False:
            return False
        if not match_session(
            umo,
            self.settings.allowed_sessions,
            self.settings.blocked_sessions,
        ):
            return False
        session = self.state.get(umo)
        if session is None:
            return self._default_session_enabled()
        return session.enabled

    def _session(self, umo: str) -> SessionState:
        return self.state.get_or_create(
            umo,
            self.settings,
            enabled=self._default_session_enabled(),
        )

    def _peek_session(self, umo: str) -> SessionState:
        """只读视图：不把状态写进内存/持久化。"""
        session = self.state.get(umo)
        if session is not None:
            return session
        return SessionState(
            enabled=self._default_session_enabled(),
            template=self.settings.default_template,
            intensity=self.settings.default_intensity,
        )

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _mutation_allowed(self, event: AstrMessageEvent) -> bool:
        if not self.settings.admin_only:
            return True
        return self._is_admin(event)

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:
                pass
        return str(getattr(event, "message_str", "") or "")

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        getter = getattr(event, "is_private_chat", None)
        if callable(getter):
            try:
                return bool(getter())
            except Exception:
                return False
        return bool(getter)

    def _numbered_for(self, umo: str) -> bool:
        session = self.state.get(umo)
        if session is not None and session.numbered is not None:
            return bool(session.numbered)
        return bool(self.settings.channel_numbered)

    @staticmethod
    def _targets_group(text: str, group: str) -> bool:
        """回复有没有真的写到目标群体（用于识别「把鱿鱼理解成海鲜」这类跑题）。"""
        markers = TOPIC_MARKERS.get(group, ())
        if not markers:
            return True
        return any(marker in text for marker in markers)

    def _channel_group(self, prompt: str) -> tuple[str, int]:
        """从用户请求里判断该走哪个通道、要几条。"""
        if not prompt:
            return "", 0
        request = detect_request(prompt)
        if request is None:
            return "", 0
        if request.category in GROUP_LABELS:
            return request.category, max(1, min(int(request.count or 5), 10))
        return "", 0

    async def _provider_id(self, umo: str) -> str:
        provider_id = self.settings.channel_provider_id.strip()
        if provider_id:
            return provider_id
        try:
            return await self.context.get_current_chat_provider_id(umo=umo)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 早期钩子：关掉流式
    # ------------------------------------------------------------------

    if EventMessageType is not None:

        @filter.event_message_type(EventMessageType.ALL)
        async def on_message_prepare(self, event: AstrMessageEvent) -> None:
            """在 LLM 阶段之前把该事件的流式输出关掉。

            ``enable_streaming`` 在 agent 子阶段一进来就会被读取，所以只能在
            更早的消息阶段设置。关掉后 ``on_llm_response`` 的替换才可见。
            """
            try:
                await self._ensure_loaded()
                if not self.settings.force_non_streaming:
                    return
                if not self._session_enabled(event.unified_msg_origin):
                    return
                event.set_extra("enable_streaming", False)
                if self.settings.hide_reasoning:
                    event.set_extra("enable_reasoning", False)
            except Exception:
                self.logger.exception("破甲：关闭流式失败")

    # ------------------------------------------------------------------
    # 请求注入
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """在请求发出前注入轻量提示，并记下原始请求供阶梯复用。"""
        try:
            await self._ensure_loaded()
            umo = event.unified_msg_origin
            if not self._session_enabled(umo):
                event.set_extra(_EXTRA_ACTIVE, False)
                return

            session = self._session(umo)
            injection = render_injection(session, self.settings)
            original_system = req.system_prompt or ""
            original_prompt = req.prompt or ""

            if self.settings.force_non_streaming:
                event.set_extra("enable_streaming", False)

            inject_mode = self.settings.inject_mode
            if inject_mode == "none":
                req.system_prompt = original_system
            elif inject_mode != "user_tail":
                req.system_prompt = apply_injection(
                    original_system,
                    injection,
                    inject_mode,
                    replace=self.settings.replace_system_prompt,
                )
            else:
                req.system_prompt = original_system

            if inject_mode in ("both", "user_tail"):
                parts = getattr(req, "extra_user_content_parts", None)
                if not isinstance(parts, list):
                    parts = list(parts or [])
                    setattr(req, "extra_user_content_parts", parts)
                try:
                    part = TextPart(text=user_tail_text()).mark_as_temp()
                except Exception:
                    part = {"type": "text", "text": user_tail_text()}
                parts.append(part)

            event.set_extra(_EXTRA_ACTIVE, True)
            event.set_extra(_EXTRA_ORIGINAL_PROMPT, original_prompt)
            event.set_extra(_EXTRA_EFFECTIVE_SYSTEM, req.system_prompt or "")
            event.set_extra(_EXTRA_INJECTION, injection)
            event.set_extra(_EXTRA_RETRY_COUNT, 0)
            event.set_extra(_EXTRA_CHANNEL_HIT, False)
            group, want = self._channel_group(original_prompt)
            event.set_extra(_EXTRA_GROUP, group)
            event.set_extra(_EXTRA_WANT, want)

            if self.settings.debug:
                self.logger.debug(
                    "破甲注入：umo=%s template=%s intensity=%s mode=%s group=%s",
                    umo,
                    session.template,
                    session.intensity,
                    self.settings.inject_mode,
                    group or "-",
                )
        except Exception:
            self.logger.exception("破甲注入失败，本次请求保持原样")

    # ------------------------------------------------------------------
    # 拒绝阶梯
    # ------------------------------------------------------------------

    async def _direct_retry(
        self,
        *,
        provider_id: str,
        prompt: str,
    ) -> str:
        """直连重试：同一个请求，换温度、换掉元指令，看模型能否自己改口。"""
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=self.settings.retry_system.strip(),
                temperature=self.settings.direct_temperature,
            ),
            timeout=self.settings.retry_timeout,
        )
        return (getattr(response, "completion_text", "") or "").strip()

    async def _channel_once(
        self,
        *,
        provider_id: str,
        group: str,
        count: int,
        temperature: float,
    ) -> str:
        request = build_request(
            group,
            count,
            rng=self._rng,
            amoral=self.settings.amoral_mode,
        )
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=request.prompt,
                system_prompt=request.system,
                temperature=temperature,
            ),
            timeout=self.settings.retry_timeout,
        )
        return (getattr(response, "completion_text", "") or "").strip()

    async def _run_channel(
        self,
        *,
        provider_id: str,
        group: str,
        count: int,
        numbered: bool = True,
    ) -> tuple[str, int, int]:
        """并发打 ``channel_attempts`` 次，必要时再来一波，按条合并结果。

        每一波用新的随机源句、温度递增。返回 ``(正文, 合规次数, 总调用次数)``。
        """
        attempts = max(1, min(int(self.settings.channel_attempts), 6))
        waves = max(1, min(int(self.settings.channel_waves), 4))
        total = 0
        passed_total = 0
        collected: list[str] = []
        for wave in range(waves):
            wave_temperature = min(
                2.0,
                self.settings.channel_temperature + 0.12 * wave,
            )
            tasks = [
                asyncio.create_task(
                    self._channel_once(
                        provider_id=provider_id,
                        group=group,
                        count=count,
                        temperature=wave_temperature,
                    ),
                )
                for _ in range(attempts)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            texts: list[str] = []
            for item in results:
                if isinstance(item, Exception):
                    self.logger.warning("破甲：通道单次失败：%s", item)
                    continue
                texts.append(item)
            total += len(texts)
            compliant = [text for text in texts if looks_compliant(text, group)[0]]
            passed_total += len(compliant)
            collected.extend(compliant)
            merged = merge_items(collected, group, count, numbered=numbered)
            got = len([line for line in merged.splitlines() if line.strip()])
            self.logger.info(
                "破甲：通道第 %s 波（温度 %.2f）合规 %s/%s，累计 %s 条",
                wave + 1,
                wave_temperature,
                len(compliant),
                len(texts),
                got,
            )
            if got >= count:
                return merged, passed_total, total
        return (
            merge_items(collected, group, count, numbered=numbered),
            passed_total,
            total,
        )

    @filter.on_llm_response()
    async def on_llm_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """模型拒绝时按阶梯重取，所有结果都来自模型。"""
        try:
            await self._ensure_loaded()
            if not event.get_extra(_EXTRA_ACTIVE, False):
                return
            if getattr(response, "is_chunk", False):
                return
            if getattr(response, "role", "assistant") != "assistant":
                return
            if getattr(response, "tools_call_args", None):
                return

            text = (response.completion_text or "").strip()
            if not text:
                return
            refused, reason = looks_like_refusal(text, self.settings.refusal_scan_chars)

            group_hint = str(event.get_extra(_EXTRA_GROUP, "") or "")
            wrong_target = False
            if not refused and group_hint in ("black", "jew", "indian"):
                markers = TOPIC_MARKERS.get(group_hint, ())
                if markers and not any(marker in text for marker in markers):
                    wrong_target = True
                    reason = f"wrong-target:{group_hint}"
            if not refused and not wrong_target:
                return

            umo = event.unified_msg_origin
            if not self._session_enabled(umo):
                return

            prompt = str(event.get_extra(_EXTRA_ORIGINAL_PROMPT, "") or "")
            group = str(event.get_extra(_EXTRA_GROUP, "") or "")
            if not group:
                # 没点名群体：没有可替换的通道，保持模型原回复
                self.logger.info("破甲：检测到拒绝（%s），但未点名群体，不做替换", reason)
                return
            want = int(event.get_extra(_EXTRA_WANT, 0) or 0) or self.settings.channel_count
            provider_id = await self._provider_id(umo)
            if not provider_id:
                self.logger.warning("破甲：拿不到 provider，跳过阶梯")
                return

            self.logger.info("破甲：检测到拒绝/跑题（%s），进入阶梯", reason)

            # 第 1 级：直连重试（只对点菜类请求有意义；跑题时不重试同一句）
            if self.settings.direct_retry and prompt and group and not wrong_target:
                try:
                    retry_text = await self._direct_retry(
                        provider_id=provider_id,
                        prompt=prompt,
                    )
                except Exception as exc:
                    self.logger.warning("破甲：直连重试失败：%s", exc)
                    retry_text = ""
                if (
                    retry_text
                    and not looks_like_refusal(
                        retry_text,
                        self.settings.refusal_scan_chars,
                    )[0]
                    and self._targets_group(retry_text, group)
                ):
                    response.completion_text = clean_output(retry_text)
                    self.logger.info("破甲：直连重试成功（模型自己改口）")
                    return

            # 第 2 级：术语表翻译通道
            if self.settings.channel_enabled and group:
                channel_text, passed, total = await self._run_channel(
                    provider_id=provider_id,
                    group=group,
                    count=want,
                    numbered=self._numbered_for(umo),
                )
                if channel_text:
                    response.completion_text = clean_output(channel_text)
                    event.set_extra(_EXTRA_CHANNEL_HIT, True)
                    self.logger.info(
                        "破甲：翻译通道命中（%s/%s），已替换为模型输出",
                        passed,
                        total,
                    )
                    return
                self.logger.warning("破甲：翻译通道 %s 次全部未命中（%s）", total, group)

            self.logger.warning("破甲：阶梯走完仍未拿到内容，保留模型原始回复")
        except Exception:
            self.logger.exception("破甲阶梯异常，保留原始回复")

    # ------------------------------------------------------------------
    # 发送前只做清理，不再替换
    # ------------------------------------------------------------------

    @filter.on_decorating_result(priority=100)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """清理模型泄露的注入标记，不改写内容。"""
        try:
            await self._ensure_loaded()
            if not event.get_extra(_EXTRA_ACTIVE, False):
                return
            result = event.get_result()
            chain = getattr(result, "chain", None) if result is not None else None
            if not chain:
                return
            text = _chain_text(chain)
            if not text.strip():
                return
            cleaned = clean_output(text)
            if cleaned and cleaned != text:
                _replace_chain_text(chain, cleaned)
        except Exception:
            self.logger.exception("破甲：发送前清理异常")

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    @filter.command("破甲", alias={"pojia", "jailbreak", "越狱"})
    async def pojia_command(self, event: AstrMessageEvent):
        """破甲控制台：开关、模板、阶梯、直接走通道。"""
        await self._ensure_loaded()
        args = self._parse_args(self._message_text(event) or event.message_str)
        if not args:
            yield event.plain_result(self._help_text())
            return

        action = args[0].strip().lower()
        rest = args[1:]

        if action in {"help", "帮助", "?", "-h", "--help"}:
            yield event.plain_result(self._help_text())
            return

        if action in {"status", "状态"}:
            yield event.plain_result(self._status_text(event.unified_msg_origin))
            return

        if action in {"list", "templates", "模板列表"}:
            yield event.plain_result(self._list_text())
            return

        if action in {"categories", "类别", "笑话列表"}:
            yield event.plain_result(self._category_text())
            return

        if action in {"编号", "number", "numbered"}:
            if not self._mutation_allowed(event):
                yield event.plain_result("需要管理员权限才能修改输出格式。")
                return
            session = self._session(event.unified_msg_origin)
            sub = rest[0].strip().lower() if rest else ""
            if sub in {"on", "开", "开启", "1", "true"}:
                session.numbered = True
            elif sub in {"off", "关", "关闭", "0", "false"}:
                session.numbered = False
            elif sub in {"reset", "默认", "follow", "继承"}:
                session.numbered = None
            else:
                current = self._numbered_for(event.unified_msg_origin)
                yield event.plain_result(
                    f"当前输出：{'带序号' if current else '不带序号（空行分隔）'}\n"
                    "用法：/破甲 编号 on|off|默认",
                )
                return
            session.touch()
            await self._save_state()
            yield event.plain_result(
                f"【破甲】输出格式已设为：{'带序号' if self._numbered_for(event.unified_msg_origin) else '不带序号（空行分隔）'}",
            )
            return

        if action in {"preview", "预览", "注入"}:
            if not self._is_admin(event):
                yield event.plain_result("需要管理员权限才能预览注入块。")
                return
            session = self._peek_session(event.unified_msg_origin)
            injection = render_injection(session, self.settings)
            amoral = "开" if self.settings.amoral_mode else "关"
            nostream = "开" if self.settings.force_non_streaming else "关"
            yield event.plain_result(
                "【破甲注入块预览】\n"
                f"模板：{self._template_label(session.template)} | "
                f"强度：{session.intensity}/3 | 长度：{len(injection)} 字符\n"
                f"无道德条款：{amoral} | 关闭流式：{nostream}\n"
                "-----\n"
                f"{injection[:2600]}",
            )
            return

        if action in {"讲", "通道", "say", "joke"}:
            if self.settings.joke_admin_only and not self._is_admin(event):
                yield event.plain_result("该命令仅限管理员。")
                return
            group = normalize_group(rest[0]) if rest else "black"
            if not group:
                yield event.plain_result(
                    f"用法：/破甲 讲 <black|jew|dark> [数量]\n类别：{category_help()}",
                )
                return
            count = self._parse_number(rest[1:]) or self.settings.channel_count
            provider_id = await self._provider_id(event.unified_msg_origin)
            if not provider_id:
                yield event.plain_result("拿不到 provider，无法调用模型。")
                return
            text, passed, total = await self._run_channel(
                provider_id=provider_id,
                group=group,
                count=count,
                numbered=self._numbered_for(event.unified_msg_origin),
            )
            if text:
                yield event.plain_result(clean_output(text))
            else:
                yield event.plain_result(
                    f"本次 {total} 次通道全部被拒（可能触发服务商侧审核），换个时间再试。",
                )
            return

        if action in {"通道测试", "probe"}:
            if not self._is_admin(event):
                yield event.plain_result("需要管理员权限。")
                return
            group = normalize_group(rest[0]) if rest else "black"
            rounds = self._parse_number(rest[1:]) or 3
            rounds = max(1, min(rounds, 10))
            provider_id = await self._provider_id(event.unified_msg_origin)
            if not provider_id:
                yield event.plain_result("拿不到 provider，无法调用模型。")
                return
            hits = 0
            lines = [f"【通道测试】{GROUP_LABELS.get(group, group)} × {rounds} 轮"]
            for index in range(rounds):
                text, passed, total = await self._run_channel(
                    provider_id=provider_id,
                    group=group,
                    count=self.settings.channel_count,
                    numbered=self._numbered_for(event.unified_msg_origin),
                )
                ok = bool(text)
                hits += int(ok)
                lines.append(
                    f"{index + 1}. {'命中' if ok else '未命中'}"
                    f"（并发 {total} 次，合规 {passed}）"
                    + (f"｜{count_items(text)} 条" if ok else ""),
                )
            lines.append(f"命中率：{hits}/{rounds}")
            yield event.plain_result("\n".join(lines))
            return

        if action in {"on", "开", "开启", "enable"}:
            if not self._mutation_allowed(event):
                yield event.plain_result("需要管理员权限才能开启破甲。")
                return
            if not self.settings.master_enabled:
                yield event.plain_result("插件总开关已关闭，请先在 WebUI 打开 master_enabled。")
                return
            if self.state.global_enabled is False:
                yield event.plain_result("全局开关处于关闭状态，请先执行 /破甲 global on。")
                return
            umo = event.unified_msg_origin
            session = self._session(umo)
            session.enabled = True
            if rest:
                name = canonical_template(rest[0])
                if name is None:
                    yield event.plain_result(
                        f"未知模板：{rest[0]}\n\n{self._list_text()}",
                    )
                    return
                session.template = name
            session.touch()
            await self._save_state()
            yield event.plain_result(
                f"【破甲】已开启\n{self._session_summary(session, umo)}",
            )
            return

        if action in {"off", "关", "关闭", "disable"}:
            if not self._mutation_allowed(event):
                yield event.plain_result("需要管理员权限才能关闭破甲。")
                return
            session = self._session(event.unified_msg_origin)
            session.enabled = False
            session.touch()
            await self._save_state()
            yield event.plain_result("【破甲】已关闭，本会话恢复默认提示词与默认拒绝行为。")
            return

        if action in {"template", "mode", "模板", "模式"}:
            if not self._mutation_allowed(event):
                yield event.plain_result("需要管理员权限才能切换模板。")
                return
            if not rest:
                yield event.plain_result(
                    f"当前模板：{self._template_label(self._peek_session(event.unified_msg_origin).template)}\n\n"
                    f"{self._list_text()}",
                )
                return
            name = canonical_template(rest[0])
            if name is None:
                yield event.plain_result(
                    f"未知模板：{rest[0]}\n\n{self._list_text()}",
                )
                return
            session = self._session(event.unified_msg_origin)
            session.template = name
            session.touch()
            await self._save_state()
            yield event.plain_result(
                f"【破甲】模板已切换为：{self._template_label(name)}\n"
                f"{self._session_summary(session, event.unified_msg_origin)}",
            )
            return

        if action in {"intensity", "level", "强度"}:
            if not self._mutation_allowed(event):
                yield event.plain_result("需要管理员权限才能调整强度。")
                return
            if not rest:
                yield event.plain_result(
                    f"当前强度：{self._peek_session(event.unified_msg_origin).intensity}/3（1 守序 / 2 中立 / 3 混沌）",
                )
                return
            try:
                value = int(rest[0])
            except (TypeError, ValueError):
                yield event.plain_result("强度必须是 1、2 或 3。")
                return
            if value < 1 or value > 3:
                yield event.plain_result("强度必须是 1、2 或 3。")
                return
            session = self._session(event.unified_msg_origin)
            session.intensity = value
            session.touch()
            await self._save_state()
            yield event.plain_result(
                f"【破甲】强度已设为：{value}/3\n"
                f"{self._session_summary(session, event.unified_msg_origin)}",
            )
            return

        if action in {"reset", "重置"}:
            if not self._mutation_allowed(event):
                yield event.plain_result("需要管理员权限才能重置。")
                return
            session = self.state.reset(
                event.unified_msg_origin,
                self.settings,
                enabled=self._default_session_enabled(),
            )
            await self._save_state()
            yield event.plain_result(
                f"【破甲】本会话已重置。\n{self._session_summary(session, event.unified_msg_origin)}",
            )
            return

        if action in {"global", "全局"}:
            if not self._is_admin(event):
                yield event.plain_result("需要管理员权限才能修改全局开关。")
                return
            if not rest:
                yield event.plain_result(
                    f"全局开关：{self._global_label()}\n"
                    "用法：/破甲 global on|off|reset",
                )
                return
            sub = rest[0].strip().lower()
            if sub in {"on", "开", "开启"}:
                self.state.global_enabled = True
            elif sub in {"off", "关", "关闭"}:
                self.state.global_enabled = False
            elif sub in {"reset", "默认", "inherit"}:
                self.state.global_enabled = None
            else:
                yield event.plain_result("用法：/破甲 global on|off|reset")
                return
            await self._save_state()
            yield event.plain_result(f"【破甲】全局开关：{self._global_label()}")
            return

        if action in {"reload", "重载"}:
            if not self._is_admin(event):
                yield event.plain_result("需要管理员权限才能重载配置。")
                return
            self._reload_settings()
            yield event.plain_result(
                "【破甲】已重新读取配置。\n"
                f"模板默认值：{self._template_label(self.settings.default_template)}，"
                f"通道：{'开' if self.settings.channel_enabled else '关'}"
                f"（并发 {self.settings.channel_attempts} × {self.settings.channel_waves} 波）。",
            )
            return

        yield event.plain_result(
            f"未知子命令：{args[0]}\n\n{self._help_text()}",
        )

    # ------------------------------------------------------------------
    # 命令辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_args(message: str) -> list[str]:
        parts = (message or "").strip().split()
        if not parts:
            return []
        first = _PREFIX_RE.sub("", parts[0]).lower().split("@", 1)[0]
        if any(first == name or first.endswith(name) for name in _COMMAND_NAMES):
            parts = parts[1:]
        return parts

    @staticmethod
    def _parse_number(items: list[str]) -> int:
        for item in items:
            if item.isdigit():
                return max(1, min(10, int(item)))
        return 0

    def _template_label(self, name: str) -> str:
        canonical = canonical_template(name) or "neutral"
        return f"{TEMPLATE_LABELS.get(canonical, canonical)} ({canonical})"

    def _global_label(self) -> str:
        if self.state.global_enabled is None:
            return f"跟随配置（{'开' if self.settings.default_enabled else '关'}）"
        return "开" if self.state.global_enabled else "关"

    def _session_summary(self, session: SessionState, umo: str) -> str:
        return (
            f"模板：{self._template_label(session.template)} | "
            f"强度：{session.intensity}/3 {INTENSITY_LABELS.get(session.intensity, '')} | "
            f"注入：{self.settings.inject_mode} | "
            f"通道：{'开' if self.settings.channel_enabled else '关'}"
            f"（{self.settings.channel_attempts} 并发 × {self.settings.channel_waves} 波）\n"
            f"会话：{umo}"
        )

    def _status_text(self, umo: str) -> str:
        session = self._peek_session(umo)
        return (
            "【破甲状态 v4】\n"
            f"插件总开关：{'开' if self.settings.master_enabled else '关'}\n"
            f"全局开关：{self._global_label()}\n"
            f"本会话：{'开' if self._session_enabled(umo) else '关'}\n"
            f"模板：{self._template_label(session.template)}\n"
            f"强度：{session.intensity}/3 {INTENSITY_LABELS.get(session.intensity, '')}\n"
            f"注入模式：{self.settings.inject_mode}\n"
            f"无道德条款：{'开' if self.settings.amoral_mode else '关'}\n"
            f"直连重试：{'开' if self.settings.direct_retry else '关'}"
            f" | 翻译通道：{'开' if self.settings.channel_enabled else '关'}"
            f"（{self.settings.channel_attempts} 并发 × {self.settings.channel_waves} 波，温度 {self.settings.channel_temperature}）\n"
            f"关闭流式：{'开' if self.settings.force_non_streaming else '关'}\n"
            f"本地库：已移除\n"
            f"会话：{umo}"
        )

    def _category_text(self) -> str:
        return (
            "【破甲 v4 通道】\n"
            "black  = 黑人/尼哥（源句含 nigger → 模型译成「黑鬼」）\n"
            "jew    = 犹太人（源句含 kike → 模型译成「犹太佬」；「鱿鱼」也叫这个组）\n"
            "indian = 印度人/阿三（源句含 pajeet → 模型译成「阿三」）\n"
            "dark   = 地狱笑话（英文源句，模型译成中文）\n"
            f"类别别名：{category_help()}\n"
            "用法：/破甲 讲 <类别> [数量]   —— 走模型\n"
            "      /破甲 通道测试 <类别> [轮数]  —— 实测命中率（管理员）\n"
            "直接发「讲个黑鬼笑话」「讲个阿三笑话」也会先交给模型；模型拒绝后自动进通道。"
        )

    def _list_text(self) -> str:
        lines = ["【破甲模板】"]
        summaries = {
            "neutral": "中立：不判断对错，按尺度交付（默认）",
            "good": "善良：有同情心、笔调克制，但不拒绝、不说教",
            "evil": "邪恶：主动加码、越狠越好，不道歉不留余地",
        }
        for name in TEMPLATE_ORDER:
            label = TEMPLATE_LABELS.get(name, name)
            lines.append(f"- {label} ({name})：{summaries.get(name, '')}")
        lines.append("\n用法：/破甲 on <模板> 或 /破甲 template <模板>")
        return "\n".join(lines)

    def _help_text(self) -> str:
        return (
            "【破甲插件 v4】拒绝阶梯：直连重试 → 术语表翻译通道（本地库已移除）\n"
            "命令（别名：/pojia、/jailbreak、/越狱）：\n"
            "/破甲 on [模板]        开启；可同时切换模板\n"
            "/破甲 off              关闭本会话\n"
            "/破甲 status           查看状态\n"
            "/破甲 讲 <类别> [n]    走模型生成（翻译通道）\n"
            "/破甲 预览             查看注入块（管理员）\n"
            "/破甲 编号 on|off      回复是否带序号\n"
            "/破甲 通道测试 <类别>  实测命中率（管理员）\n"
            "/破甲 template <模板>  切换模板\n"
            "/破甲 intensity <1-3>  调整强度\n"
            "/破甲 list             列出模板\n"
            "/破甲 类别             通道类别\n"
            "/破甲 reset            重置本会话\n"
            "/破甲 global on|off|reset  全局开关（管理员）\n"
            "/破甲 reload           重新读取 WebUI 配置（管理员）\n\n"
            f"通道：{'开' if self.settings.channel_enabled else '关'}"
            f"（{self.settings.channel_attempts} 并发 × {self.settings.channel_waves} 波）"
        )












