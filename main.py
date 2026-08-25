import time
import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain, At

from .easybot_adapter import EasyBotAdapter
from .member_checker import MemberChecker, KickManager, ScanReport, InactiveMemberInfo

logger = logging.getLogger("astrbot")

class EasyBotCleanerPlugin(Star):
    """
    EasyBot 联动 MC 绑定与潜水清理插件
    """

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.adapter = EasyBotAdapter(self.config)
        self.checker = MemberChecker(self.config)
        self.kick_mgr = KickManager(self.config)

        # 待确认清理缓存: { group_id: { "timestamp": float, "days": int, "candidates": list } }
        self._pending_cleans: Dict[str, Dict[str, Any]] = {}

        # 启动后台定时巡检任务
        self._scheduler_task: Optional[asyncio.Task] = None
        if self.config.get("auto_clean_enabled", False):
            self._scheduler_task = asyncio.create_task(self._scheduled_loop())
            logger.info("[EasyBotCleaner] 已启动定时自动巡检后台任务")

    async def terminate(self):
        """插件卸载或重载时的清理"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            logger.info("[EasyBotCleaner] 定时后台任务已终止")

    # ---------------- 辅助方法 ----------------

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        """安全提取群号"""
        if hasattr(event, "get_group_id") and callable(event.get_group_id):
            gid = event.get_group_id()
            if gid:
                return str(gid)
        if hasattr(event, "message_obj") and event.message_obj:
            gid = getattr(event.message_obj, "group_id", None)
            if gid:
                return str(gid)
        return None

    def _get_sender_id(self, event: AstrMessageEvent) -> Optional[str]:
        """安全提取发送者 QQ"""
        if hasattr(event, "get_sender_id") and callable(event.get_sender_id):
            sid = event.get_sender_id()
            if sid:
                return str(sid)
        if hasattr(event, "message_obj") and event.message_obj:
            sender = getattr(event.message_obj, "sender", None)
            if sender:
                sid = getattr(sender, "user_id", None)
                if sid:
                    return str(sid)
        return None

    def _get_bot_id(self, event: AstrMessageEvent) -> Optional[str]:
        """提取 Bot 自身的 QQ"""
        if hasattr(event, "bot") and event.bot:
            if hasattr(event.bot, "self_id") and event.bot.self_id:
                return str(event.bot.self_id)
        return None

    async def _get_group_members(self, event: AstrMessageEvent, group_id: str) -> List[Dict[str, Any]]:
        """获取群成员列表（支持 OneBot / NapCat / Lagrange 等协议端）"""
        gid = int(group_id) if str(group_id).isdigit() else group_id
        bot = event.bot

        try:
            if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                res = await bot.api.call_action("get_group_member_list", group_id=gid, no_cache=True)
                if isinstance(res, list):
                    return res
                if isinstance(res, dict) and "data" in res:
                    return res["data"]
            elif hasattr(bot, "get_group_member_list"):
                res = await bot.get_group_member_list(group_id=gid, no_cache=True)
                if isinstance(res, list):
                    return res
            elif hasattr(bot, "call_action"):
                res = await bot.call_action("get_group_member_list", group_id=gid, no_cache=True)
                if isinstance(res, list):
                    return res
        except Exception as e:
            logger.error(f"[EasyBotCleaner] 获取群 {group_id} 成员列表失败: {e}", exc_info=True)
        return []

    async def _check_permission(self, event: AstrMessageEvent, group_id: Optional[str] = None) -> bool:
        """验证发送者是否具有管理权限（群主/管理员/AstrBot超管）"""
        sender_id = self._get_sender_id(event)
        if not sender_id:
            return False

        # 1. 如果是 AstrBot 管理员 / 主人
        if hasattr(event, "is_admin") and callable(event.is_admin):
            if event.is_admin():
                return True

        # 2. 如果没有 group_id（私聊场景），且不是 AstrBot 管理员，则直接拒绝
        if not group_id:
            return False

        # 3. 优先从消息事件自带的 sender 信息中提取角色（0 延迟，适配 OneBot/NapCat/Lagrange）
        if hasattr(event, "message_obj") and event.message_obj:
            sender = getattr(event.message_obj, "sender", None)
            if sender:
                role = str(getattr(sender, "role", "") or "").lower()
                if role in ["owner", "admin"]:
                    return True

        # 4. 兜底方案：查询群成员列表验证角色
        members = await self._get_group_members(event, group_id)
        for m in members:
            if str(m.get("user_id")) == str(sender_id):
                role = str(m.get("role", "member")).lower()
                if role in ["owner", "admin"]:
                    return True
                break
        return False

    def _is_group_allowed(self, group_id: str) -> bool:
        """检查群是否在目标群列表中"""
        target_groups = self.config.get("target_groups", [])
        if not target_groups:
            return True
        return str(group_id) in [str(g) for g in target_groups]

    # ---------------- 核心指令实现 ----------------

    @filter.command("mc扫描")
    async def mc_scan_cmd(self, event: AstrMessageEvent, days: Optional[int] = None):
        """
        /mc扫描 [未发言天数] - 扫描未绑定 MC 且超期未发言的成员名单（预览模式，不踢人）
        """
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 本指令仅支持在 QQ 群内使用！")
            return

        if not self._is_group_allowed(group_id):
            yield event.plain_result("⚠️ 本群未开启 MC 潜水清理功能。")
            return

        if not await self._check_permission(event, group_id):
            yield event.plain_result("⛔ 权限不足：只有群主或管理员才可执行此指令！")
            return

        threshold_days = days if days is not None and days > 0 else int(self.config.get("default_inactive_days", 30))
        yield event.plain_result(f"🔍 正在查询 EasyBot 绑定数据并扫描群成员 (未发言阈值: {threshold_days} 天)...")

        # 1. 获取已绑定 QQ 集合
        bound_qqs = await self.adapter.get_bound_qq_set()
        logger.info(f"[mc扫描] 获取到 {len(bound_qqs)} 个已绑定 MC 账号")

        # 2. 获取群成员列表
        members = await self._get_group_members(event, group_id)
        if not members:
            yield event.plain_result("❌ 获取群成员列表失败，请检查机器人是否具有群管权限或协议端连接正常。")
            return

        # 3. 运行过滤算法
        bot_id = self._get_bot_id(event)
        report = self.checker.filter_inactive_members(
            group_id=group_id,
            member_list=members,
            bound_qq_set=bound_qqs,
            bot_self_id=bot_id,
            override_inactive_days=threshold_days
        )

        # 4. 生成格式化报告
        msg_lines = [
            f"📊【群潜水成员扫描报告】",
            f"━━━━━━━━━━━━━━━━━━",
            f"👥 群总人数: {report.total_members} 人",
            f"🎮 已绑定 MC 账号: {report.bound_count} 人 (已豁免)",
            f"🛡️ 管理员/白名单: {report.exempt_count} 人 (已豁免)",
            f"🌱 新人保护期 (<{report.grace_days}天): {report.grace_count} 人 (已豁免)",
            f"💬 未绑定近期发言: {report.active_unbound_count} 人",
            f"━━━━━━━━━━━━━━━━━━",
            f"⚠️ 待清理人数 (未绑定且 ≥{report.threshold_days}天未发言): {len(report.inactive_candidates)} 人",
        ]

        if not report.inactive_candidates:
            msg_lines.append("\n🎉 太棒了！群内暂无超期未绑定潜水成员。")
        else:
            msg_lines.append("\n📋 待清理成员名单 (最多展示前 30 名):")
            for idx, c in enumerate(report.inactive_candidates[:30], 1):
                if c.last_sent_time > 0:
                    status_text = f"最后发言: {c.days_since_last_sent}天前"
                else:
                    status_text = f"入群{c.days_since_join}天从未发言"
                msg_lines.append(f"{idx}. {c.display_name} ({c.user_id}) - {status_text}")

            if len(report.inactive_candidates) > 30:
                msg_lines.append(f"... 还有 {len(report.inactive_candidates) - 30} 人未展示")

            msg_lines.append("\n💡 提示: 若要执行清理，请发送: /mc清理 " + str(report.threshold_days))

        yield event.plain_result("\n".join(msg_lines))

    @filter.command("mc清理")
    async def mc_clean_cmd(self, event: AstrMessageEvent, days: Optional[int] = None, confirm_flag: Optional[str] = None):
        """
        /mc清理 [未发言天数] [确认/强制] - 清理未绑定且超期未发言的成员
        """
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 本指令仅支持在 QQ 群内使用！")
            return

        if not self._is_group_allowed(group_id):
            yield event.plain_result("⚠️ 本群未开启 MC 潜水清理功能。")
            return

        if not await self._check_permission(event, group_id):
            yield event.plain_result("⛔ 权限不足：只有群主或管理员才可执行此指令！")
            return

        threshold_days = days if days is not None and days > 0 else int(self.config.get("default_inactive_days", 30))
        is_force = confirm_flag and confirm_flag.lower() in ["force", "confirm", "yes", "true", "-f", "确认", "强制", "确定"]

        now = time.time()
        pending = self._pending_cleans.get(group_id)

        # 检查是否是在 60 秒内进行的二次确认
        is_confirmed = False
        if is_force:
            is_confirmed = True
        elif pending and (now - pending["timestamp"]) < 60 and pending["days"] == threshold_days:
            is_confirmed = True

        # 1. 执行扫描
        bound_qqs = await self.adapter.get_bound_qq_set()
        members = await self._get_group_members(event, group_id)
        if not members:
            yield event.plain_result("❌ 获取群成员列表失败，无法执行清理！")
            return

        bot_id = self._get_bot_id(event)
        report = self.checker.filter_inactive_members(
            group_id=group_id,
            member_list=members,
            bound_qq_set=bound_qqs,
            bot_self_id=bot_id,
            override_inactive_days=threshold_days
        )

        candidates = report.inactive_candidates
        if not candidates:
            yield event.plain_result(f"🎉 扫描完毕，当前群内没有未绑定且超过 {threshold_days} 天未发言的成员，无需清理。")
            return

        # 若未确认，则保存待清理状态并提示二次确认
        if not is_confirmed:
            self._pending_cleans[group_id] = {
                "timestamp": now,
                "days": threshold_days,
                "candidates": candidates
            }
            yield event.plain_result(
                f"⚠️【高危操作警告】\n"
                f"经检测，群内共有 {len(candidates)} 名未绑定 MC 账号且 ≥{threshold_days} 天未发言的成员！\n\n"
                f"❗ 执行清理将自动将这些成员踢出群聊。\n"
                f"👉 请在 60 秒内发送: /mc清理 {threshold_days} 确认 确认执行！"
            )
            return

        # 已确认，开始执行踢人流程
        self._pending_cleans.pop(group_id, None)
        interval = float(self.config.get("kick_interval_seconds", 1.5))
        est_seconds = round(len(candidates) * interval)
        yield event.plain_result(f"🚀 开始执行清理任务，共 {len(candidates)} 人待踢出，预计耗时 {est_seconds} 秒 (防风控间隔 {interval}s)...")

        success_list, fail_list = await self.kick_mgr.execute_kick_batch(
            bot=event.bot,
            group_id=group_id,
            candidates=candidates,
            interval_seconds=interval
        )

        res_msg = [
            f"✅【群清理执行完成】",
            f"━━━━━━━━━━━━━━━━━━",
            f"🎯 待清理总数: {len(candidates)} 人",
            f"✔️ 成功踢出: {len(success_list)} 人",
            f"❌ 踢出失败: {len(fail_list)} 人",
        ]
        if fail_list:
            res_msg.append("\n⚠️ 失败原因汇总 (前5条):")
            for m, err in fail_list[:5]:
                res_msg.append(f"- {m.display_name}({m.user_id}): {err}")
            res_msg.append("💡 请确认机器人是否具备群管理员权限，且被踢成员角色不高于机器人！")

        yield event.plain_result("\n".join(res_msg))

    @filter.command("mc查绑定")
    async def mc_bind_check_cmd(self, event: AstrMessageEvent, target_qq: Optional[str] = None):
        """
        /mc查绑定 [QQ号 / @成员] - 查询单个成员的 MC 绑定状态和群活跃记录
        """
        group_id = self._get_group_id(event)
        sender_id = self._get_sender_id(event)

        # 尝试从 At 中解析
        query_qq = None
        if hasattr(event, "message_obj") and event.message_obj and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    query_qq = str(comp.qq)
                    break

        if not query_qq:
            if target_qq and str(target_qq).strip().isdigit():
                query_qq = str(target_qq).strip()
            else:
                query_qq = sender_id

        if not query_qq:
            yield event.plain_result("❌ 请输入要查询的 QQ 号或 @群成员！")
            return

        yield event.plain_result(f"🔍 正在查询 QQ: {query_qq} 的绑定与发言状态...")

        # 查询 MC 绑定
        player_name = await self.adapter.get_player_by_qq(query_qq)

        # 查询群内成员信息
        member_info = None
        if group_id:
            members = await self._get_group_members(event, group_id)
            for m in members:
                if str(m.get("user_id")) == str(query_qq):
                    member_info = m
                    break

        lines = [
            f"📋【MC 绑定与活跃查询】",
            f"QQ 账号: {query_qq}",
            f"🎮 绑定状态: " + (f"已绑定 (游戏名: {player_name})" if player_name else "❌ 未绑定 MC 账号"),
        ]

        if member_info:
            now = int(time.time())
            join_time = int(member_info.get("join_time", 0))
            last_sent_time = int(member_info.get("last_sent_time", 0))
            card = member_info.get("card", "") or member_info.get("nickname", "")
            role = member_info.get("role", "member")

            join_days = round((now - join_time) / 86400, 1) if join_time > 0 else 0
            sent_days = round((now - last_sent_time) / 86400, 1) if last_sent_time > 0 else None

            lines.append(f"👤 群内昵称: {card}")
            lines.append(f"🔰 群内身份: {role}")
            lines.append(f"📅 入群时长: {join_days} 天")
            if sent_days is not None:
                lines.append(f"💬 最后发言: {sent_days} 天前")
            else:
                lines.append("💬 最后发言: 从未发言")
        else:
            lines.append("ℹ️ 未在当前群中找到该成员的群信息。")

        yield event.plain_result("\n".join(lines))

    @filter.command("mc白名单")
    async def mc_whitelist_cmd(self, event: AstrMessageEvent, action: Optional[str] = None, qq: Optional[str] = None):
        """
        /mc白名单 [添加/删除/列表] [QQ号] - 管理永久豁免白名单
        """
        group_id = self._get_group_id(event)
        if not await self._check_permission(event, group_id):
            yield event.plain_result("⛔ 权限不足：只有管理员可配置白名单！")
            return

        act = (action or "列表").lower()
        whitelist: List[str] = self.config.get("whitelist_qqs", [])
        if not isinstance(whitelist, list):
            whitelist = []

        if act in ["list", "列表", "查看", "查"]:
            if not whitelist:
                yield event.plain_result("📋 当前白名单为空（所有未绑定且潜水成员均受清理规则约束）。")
            else:
                msg = "📋【当前永久豁免白名单】\n" + "\n".join([f"- {q}" for q in whitelist])
                yield event.plain_result(msg)
            return

        if not qq or not str(qq).strip().isdigit():
            yield event.plain_result("❌ 请输入正确的 QQ 号！例如: /mc白名单 添加 123456789")
            return

        qq_str = str(qq).strip()

        if act in ["add", "+", "添加", "增加", "加"]:
            if qq_str in whitelist:
                yield event.plain_result(f"ℹ️ QQ {qq_str} 已在白名单中。")
            else:
                whitelist.append(qq_str)
                self.config["whitelist_qqs"] = whitelist
                yield event.plain_result(f"✅ 成功将 QQ {qq_str} 添加至白名单！")
        elif act in ["del", "rm", "remove", "-", "删除", "移除", "删"]:
            if qq_str in whitelist:
                whitelist.remove(qq_str)
                self.config["whitelist_qqs"] = whitelist
                yield event.plain_result(f"✅ 成功将 QQ {qq_str} 从白名单中移除！")
            else:
                yield event.plain_result(f"ℹ️ QQ {qq_str} 不在白名单中。")
        else:
            yield event.plain_result("❌ 未知操作，可用操作: /mc白名单 [添加/删除/列表] [QQ号]")

    @filter.command("mc帮助")
    async def mc_help_cmd(self, event: AstrMessageEvent):
        """
        /mc帮助 - 查看 MC 潜水清理插件帮助与当前配置
        """
        st = self.adapter.source_type
        default_days = self.config.get("default_inactive_days", 30)
        grace_days = self.config.get("new_member_grace_days", 3)
        auto_clean = self.config.get("auto_clean_enabled", False)

        help_text = (
            f"📖【EasyBot MC 绑定与潜水清理插件帮助】\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔹 /mc扫描 [天数] - 扫描未绑定且超期未发言成员（安全预览）\n"
            f"🔹 /mc清理 [天数] [确认/强制] - 清理踢出未绑定超期成员\n"
            f"🔹 /mc查绑定 [QQ/@] - 查询成员 MC 绑定与发言活跃\n"
            f"🔹 /mc白名单 [添加/删除/列表] [QQ] - 管理豁免白名单\n"
            f"🔹 /mc帮助 - 显示本帮助信息\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ 当前配置状态:\n"
            f"- 数据源类型: {st}\n"
            f"- 默认未发言天数: {default_days} 天\n"
            f"- 新人保护天数: {grace_days} 天\n"
            f"- 定时巡检任务: {'已开启' if auto_clean else '已关闭'}"
        )
        yield event.plain_result(help_text)

    # ---------------- 定时巡检后台任务 ----------------

    async def _scheduled_loop(self):
        """每天在指定小时和分钟执行一次巡检"""
        while True:
            try:
                now_dt = datetime.now()
                target_hour = int(self.config.get("auto_clean_hour", 4))
                target_minute = int(self.config.get("auto_clean_minute", 0))

                if now_dt.hour == target_hour and now_dt.minute == target_minute:
                    logger.info("[EasyBotCleaner] 触发定时巡检任务...")
                    await self._run_scheduled_clean()
                    # 避免在同一分钟内重复触发
                    await asyncio.sleep(65)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[EasyBotCleaner] 定时巡检执行异常: {e}", exc_info=True)

            await asyncio.sleep(30)

    async def _run_scheduled_clean(self):
        """执行定时自动巡检逻辑"""
        target_groups = self.config.get("target_groups", [])
        if not target_groups:
            logger.info("[EasyBotCleaner] 未配置 target_groups，定时任务跳过执行")
            return

        bound_qqs = await self.adapter.get_bound_qq_set()
        threshold_days = int(self.config.get("default_inactive_days", 30))
        mode = self.config.get("auto_clean_mode", "notify_only")
        interval = float(self.config.get("kick_interval_seconds", 1.5))

        # 获取平台适配器
        # 遍历 target_groups 进行检测
        for gid in target_groups:
            gid_str = str(gid)
            try:
                # 尝试通过 context 寻找可用的平台 adapter 发起调用
                # 此处记录日志并保留扩展
                logger.info(f"[EasyBotCleaner] 正在对群 {gid_str} 执行定时巡检 (模式: {mode})...")
            except Exception as e:
                logger.error(f"[EasyBotCleaner] 定时巡检群 {gid_str} 失败: {e}")
