import time
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Dict, Set, Optional, Any, Tuple

logger = logging.getLogger("astrbot")

@dataclass
class InactiveMemberInfo:
    user_id: str
    nickname: str
    card: str
    role: str
    join_time: int
    last_sent_time: int
    days_since_last_sent: Optional[float]
    days_since_join: float
    reason: str

    @property
    def display_name(self) -> str:
        return self.card.strip() or self.nickname.strip() or self.user_id

@dataclass
class ScanReport:
    group_id: str
    total_members: int
    bound_count: int
    exempt_count: int  # 管理员/群主/Bot/白名单
    grace_count: int   # 新人保护期
    active_unbound_count: int # 未绑定但近期发言
    inactive_candidates: List[InactiveMemberInfo]
    threshold_days: int
    grace_days: int

class MemberChecker:
    """
    群成员过滤与潜水检测器
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def get_whitelist_set(self) -> Set[str]:
        raw_list = self.config.get("whitelist_qqs", [])
        if isinstance(raw_list, list):
            return {str(x).strip() for x in raw_list if str(x).strip()}
        return set()

    def filter_inactive_members(
        self,
        group_id: str,
        member_list: List[Dict[str, Any]],
        bound_qq_set: Set[str],
        bot_self_id: Optional[str] = None,
        override_inactive_days: Optional[int] = None
    ) -> ScanReport:
        """
        根据配置和绑定列表过滤出待清理成员
        """
        now = int(time.time())
        inactive_days_threshold = override_inactive_days if override_inactive_days is not None else int(self.config.get("default_inactive_days", 30))
        grace_days = int(self.config.get("new_member_grace_days", 3))
        whitelist = self.get_whitelist_set()

        inactive_seconds = inactive_days_threshold * 86400
        grace_seconds = grace_days * 86400

        total_members = len(member_list)
        bound_count = 0
        exempt_count = 0
        grace_count = 0
        active_unbound_count = 0
        candidates: List[InactiveMemberInfo] = []

        bot_self_id_str = str(bot_self_id).strip() if bot_self_id else ""

        for m in member_list:
            uid = str(m.get("user_id", "")).strip()
            if not uid:
                continue

            role = str(m.get("role", "member")).lower()
            nickname = str(m.get("nickname", ""))
            card = str(m.get("card", ""))
            join_time = int(m.get("join_time", 0))
            last_sent_time = int(m.get("last_sent_time", 0))

            # 1. 豁免检查: 群主、管理员、机器人自身、自定义白名单
            if role in ["owner", "admin"] or uid == bot_self_id_str or uid in whitelist:
                exempt_count += 1
                continue

            # 2. 新人保护期检查
            days_since_join = (now - join_time) / 86400 if join_time > 0 else 999.0
            if join_time > 0 and (now - join_time) < grace_seconds:
                grace_count += 1
                continue

            # 3. MC 绑定检查
            if uid in bound_qq_set:
                bound_count += 1
                continue

            # 4. 未发言时间检测
            days_since_last_sent = None
            if last_sent_time > 0:
                inactive_sec = now - last_sent_time
                days_since_last_sent = inactive_sec / 86400
                if inactive_sec >= inactive_seconds:
                    candidates.append(InactiveMemberInfo(
                        user_id=uid,
                        nickname=nickname,
                        card=card,
                        role=role,
                        join_time=join_time,
                        last_sent_time=last_sent_time,
                        days_since_last_sent=round(days_since_last_sent, 1),
                        days_since_join=round(days_since_join, 1),
                        reason=f"未绑定MC账号，且已 {round(days_since_last_sent, 1)} 天未发言"
                    ))
                else:
                    active_unbound_count += 1
            else:
                # 从未在群内发言
                if join_time > 0 and (now - join_time) >= inactive_seconds:
                    candidates.append(InactiveMemberInfo(
                        user_id=uid,
                        nickname=nickname,
                        card=card,
                        role=role,
                        join_time=join_time,
                        last_sent_time=0,
                        days_since_last_sent=None,
                        days_since_join=round(days_since_join, 1),
                        reason=f"未绑定MC账号，入群 {round(days_since_join, 1)} 天从未发言"
                    ))
                elif join_time == 0:
                    # 历史成员无入群记录且从未发言
                    candidates.append(InactiveMemberInfo(
                        user_id=uid,
                        nickname=nickname,
                        card=card,
                        role=role,
                        join_time=0,
                        last_sent_time=0,
                        days_since_last_sent=None,
                        days_since_join=0.0,
                        reason="未绑定MC账号，从未在群内发言"
                    ))
                else:
                    active_unbound_count += 1

        # 按最后发言时间或入群时间从远到近排序
        candidates.sort(key=lambda x: (x.last_sent_time, x.join_time))

        return ScanReport(
            group_id=str(group_id),
            total_members=total_members,
            bound_count=bound_count,
            exempt_count=exempt_count,
            grace_count=grace_count,
            active_unbound_count=active_unbound_count,
            inactive_candidates=candidates,
            threshold_days=inactive_days_threshold,
            grace_days=grace_days
        )


class KickManager:
    """
    群踢人执行器（带防风控延时与权限错误捕获）
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    async def execute_kick_batch(
        self,
        bot: Any,
        group_id: str,
        candidates: List[InactiveMemberInfo],
        interval_seconds: Optional[float] = None
    ) -> Tuple[List[InactiveMemberInfo], List[Tuple[InactiveMemberInfo, str]]]:
        """
        批量执行踢人
        返回: (成功列表, 失败列表[(成员, 错误信息)])
        """
        if interval_seconds is None:
            interval_seconds = float(self.config.get("kick_interval_seconds", 1.5))

        success_list: List[InactiveMemberInfo] = []
        fail_list: List[Tuple[InactiveMemberInfo, str]] = []

        gid = int(group_id) if str(group_id).isdigit() else group_id

        for idx, member in enumerate(candidates):
            uid = int(member.user_id) if str(member.user_id).isdigit() else member.user_id
            try:
                # 调用 OneBot / 适配器底层 kick 动作
                if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                    await bot.api.call_action("set_group_kick", group_id=gid, user_id=uid, reject_add_request=False)
                elif hasattr(bot, "set_group_kick"):
                    await bot.set_group_kick(group_id=gid, user_id=uid, reject_add_request=False)
                elif hasattr(bot, "call_action"):
                    await bot.call_action("set_group_kick", group_id=gid, user_id=uid, reject_add_request=False)
                else:
                    raise RuntimeError("无法找到可用的 set_group_kick 接口")

                success_list.append(member)
                logger.info(f"[KickManager] 已将未绑定潜水成员踢出: {member.display_name}({member.user_id})")
            except Exception as e:
                err_msg = str(e)
                logger.error(f"[KickManager] 踢出成员 {member.display_name}({member.user_id}) 失败: {err_msg}")
                fail_list.append((member, err_msg))

            # 防风控延时（最后一人不需要延时）
            if idx < len(candidates) - 1 and interval_seconds > 0:
                await asyncio.sleep(interval_seconds)

        return success_list, fail_list
