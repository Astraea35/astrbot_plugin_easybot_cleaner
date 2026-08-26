import time
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Dict, Set, Optional, Any, Tuple, Union

try:
    from .easybot_adapter import BindingDetail, parse_and_format_time, get_config_val
except ImportError:
    from easybot_adapter import BindingDetail, parse_and_format_time, get_config_val

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
    member_type: str = "unbound" # "unbound" 或 "mc_inactive"
    mc_player_name: Optional[str] = None
    days_since_last_play: Optional[float] = None

    @property
    def display_name(self) -> str:
        return self.card.strip() or self.nickname.strip() or self.user_id

@dataclass
class ScanReport:
    group_id: str
    total_members: int
    bound_count: int               # 已绑定且活跃人数 (已豁免)
    exempt_count: int              # 管理员/群主/Bot/白名单 (已豁免)
    grace_count: int               # 新人保护期 (已豁免)
    active_unbound_count: int      # 未绑定但近期发言 (未超期)
    mc_inactive_count: int         # 已绑定但 MC 长期未上线人数
    inactive_candidates: List[InactiveMemberInfo]
    threshold_days: int            # 未绑定未发言天数阈值
    grace_days: int                # 新人保护天数
    mc_clean_enabled: bool = False # 是否开启 MC 长期未上线清理
    mc_threshold_days: int = 30    # MC 长期未上线天数阈值

class MemberChecker:
    """
    群成员过滤与潜水检测器
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def _cfg_get(self, key: str, default: Any = None) -> Any:
        return get_config_val(self.config, key, default)

    def get_whitelist_set(self) -> Set[str]:
        raw_list = self._cfg_get("whitelist_qqs", [])
        if isinstance(raw_list, list):
            return {str(x).strip() for x in raw_list if str(x).strip()}
        return set()

    def filter_inactive_members(
        self,
        group_id: str,
        member_list: List[Dict[str, Any]],
        bound_qq_set: Union[Set[str], Dict[str, Any]],
        bot_self_id: Optional[str] = None,
        override_inactive_days: Optional[int] = None,
        override_mc_inactive_days: Optional[int] = None,
        override_mc_clean_enabled: Optional[bool] = None
    ) -> ScanReport:
        """
        根据配置和绑定列表过滤出待清理成员
        """
        now = int(time.time())
        inactive_days_threshold = override_inactive_days if override_inactive_days is not None else int(self._cfg_get("default_inactive_days", 30))
        grace_days = int(self._cfg_get("new_member_grace_days", 3))
        whitelist = self.get_whitelist_set()

        mc_clean_enabled = override_mc_clean_enabled if override_mc_clean_enabled is not None else self._cfg_get("mc_inactive_clean_enabled", False)
        mc_inactive_days = override_mc_inactive_days if override_mc_inactive_days is not None else int(self._cfg_get("mc_inactive_days", 30))

        inactive_seconds = inactive_days_threshold * 86400
        grace_seconds = grace_days * 86400

        total_members = len(member_list)
        bound_count = 0
        exempt_count = 0
        grace_count = 0
        active_unbound_count = 0
        mc_inactive_count = 0
        candidates: List[InactiveMemberInfo] = []

        bot_self_id_str = str(bot_self_id).strip() if bot_self_id else ""

        # 解析 bound 映射
        bound_map: Dict[str, Any] = {}
        if isinstance(bound_qq_set, dict):
            bound_map = {str(k).strip(): v for k, v in bound_qq_set.items()}
            bound_set = set(bound_map.keys())
        elif isinstance(bound_qq_set, (set, list, tuple)):
            bound_set = {str(x).strip() for x in bound_qq_set if str(x).strip()}
        else:
            bound_set = set()

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
            if uid in bound_set:
                if mc_clean_enabled:
                    detail = bound_map.get(uid)
                    p_name = getattr(detail, "player_name", None) if detail else None
                    if not p_name and isinstance(detail, dict):
                        p_name = detail.get("player_name")
                    if not p_name and isinstance(detail, str):
                        p_name = detail
                    if not p_name:
                        p_name = "已绑定"

                    raw_last_play = getattr(detail, "last_play_time", None) if detail else None
                    if not raw_last_play and isinstance(detail, dict):
                        raw_last_play = detail.get("last_play_time")

                    raw_first_bound = getattr(detail, "first_bound_time", None) if detail else None
                    if not raw_first_bound and isinstance(detail, dict):
                        raw_first_bound = detail.get("first_bound_time")

                    if raw_last_play:
                        _, _, lp_days = parse_and_format_time(raw_last_play)
                        if lp_days is not None and lp_days >= mc_inactive_days:
                            mc_inactive_count += 1
                            candidates.append(InactiveMemberInfo(
                                user_id=uid,
                                nickname=nickname,
                                card=card,
                                role=role,
                                join_time=join_time,
                                last_sent_time=last_sent_time,
                                days_since_last_sent=round((now - last_sent_time) / 86400, 1) if last_sent_time > 0 else None,
                                days_since_join=round(days_since_join, 1),
                                reason=f"已绑定MC账号({p_name})，但在游戏内已 {round(lp_days, 1)} 天未上线",
                                member_type="mc_inactive",
                                mc_player_name=p_name,
                                days_since_last_play=round(lp_days, 1)
                            ))
                        else:
                            bound_count += 1
                    elif raw_first_bound:
                        _, _, fb_days = parse_and_format_time(raw_first_bound)
                        if fb_days is not None and fb_days >= mc_inactive_days:
                            mc_inactive_count += 1
                            candidates.append(InactiveMemberInfo(
                                user_id=uid,
                                nickname=nickname,
                                card=card,
                                role=role,
                                join_time=join_time,
                                last_sent_time=last_sent_time,
                                days_since_last_sent=round((now - last_sent_time) / 86400, 1) if last_sent_time > 0 else None,
                                days_since_join=round(days_since_join, 1),
                                reason=f"已绑定MC账号({p_name})，绑定已 {round(fb_days, 1)} 天但从未上线游玩",
                                member_type="mc_inactive",
                                mc_player_name=p_name,
                                days_since_last_play=round(fb_days, 1)
                            ))
                        else:
                            bound_count += 1
                    else:
                        bound_count += 1
                else:
                    bound_count += 1
                continue

            # 4. 未发言时间检测 (针对未绑定成员)
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
                        reason=f"未绑定MC账号，且已 {round(days_since_last_sent, 1)} 天未发言",
                        member_type="unbound"
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
                        reason=f"未绑定MC账号，入群 {round(days_since_join, 1)} 天从未发言",
                        member_type="unbound"
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
                        reason="未绑定MC账号，从未在群内发言",
                        member_type="unbound"
                    ))
                else:
                    active_unbound_count += 1

        # 排序
        candidates.sort(key=lambda x: (x.last_sent_time, x.join_time))

        return ScanReport(
            group_id=str(group_id),
            total_members=total_members,
            bound_count=bound_count,
            exempt_count=exempt_count,
            grace_count=grace_count,
            active_unbound_count=active_unbound_count,
            mc_inactive_count=mc_inactive_count,
            inactive_candidates=candidates,
            threshold_days=inactive_days_threshold,
            grace_days=grace_days,
            mc_clean_enabled=mc_clean_enabled,
            mc_threshold_days=mc_inactive_days
        )


class KickManager:
    """
    群踢人执行器（带防风控延时与权限错误捕获）
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def _cfg_get(self, key: str, default: Any = None) -> Any:
        return get_config_val(self.config, key, default)

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
            interval_seconds = float(self._cfg_get("kick_interval_seconds", 1.5))

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
