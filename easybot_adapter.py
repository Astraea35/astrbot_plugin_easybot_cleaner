import os
import re
import json
import sqlite3
import logging
import asyncio
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, Set, Optional, Tuple, Any, List

logger = logging.getLogger("astrbot")


def parse_and_format_time(raw_val: Any) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    """
    解析各种格式的时间戳/日期字符串，并返回 (格式化时间串, 相对时间串, 距今天数)
    支持:
      - ISO 8601 字符串 (例如 EasyBot 2.0 的 '2026-08-25T23:05:28.3503607' 或 '2026-08-25T23:05:28Z')
      - 标准 SQL 字符串 (例如 '2026-08-25 23:05:28')
      - Unix 时间戳 (秒或毫秒级整数/浮点数)
      - datetime 对象
    返回: (formatted_str, relative_str, days_diff)
    例如: ('2026-08-25 23:05:28', '1天前', 1.0)
    """
    if raw_val is None:
        return None, None, None

    dt: Optional[datetime] = None

    if isinstance(raw_val, datetime):
        dt = raw_val
    elif isinstance(raw_val, (int, float)):
        val_float = float(raw_val)
        if val_float <= 0:
            return None, None, None
        # 判断是毫秒还是秒
        if val_float > 1e11:
            val_float = val_float / 1000.0
        try:
            dt = datetime.fromtimestamp(val_float)
        except Exception:
            return None, None, None
    elif isinstance(raw_val, str):
        val_str = raw_val.strip()
        if not val_str or val_str.lower() in ["null", "none", "0", ""]:
            return None, None, None

        # 尝试纯数字时间戳字符串
        try:
            if re.match(r"^\d+(\.\d+)?$", val_str):
                num = float(val_str)
                if num > 1e11:
                    num /= 1000.0
                dt = datetime.fromtimestamp(num)
        except Exception:
            pass

        if dt is None:
            # 兼容 .NET 7位微秒截断至6位以符合 Python fromisoformat
            # 例如 2026-08-25T23:05:28.3503607 -> 2026-08-25T23:05:28.350360
            clean_str = val_str
            clean_str = re.sub(r"(\.\d{6})\d+", r"\1", clean_str)

            # 尝试 ISO 格式
            try:
                dt = datetime.fromisoformat(clean_str)
            except Exception:
                pass

        if dt is None:
            # 常见格式尝试
            date_patterns = [
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S.%f",
                "%Y-%m-%d",
                "%Y/%m/%d",
            ]
            for pat in date_patterns:
                try:
                    dt = datetime.strptime(val_str, pat)
                    break
                except Exception:
                    continue

    if not dt:
        # 无法解析为标准 datetime 时，保留原字符串展示
        val_display = str(raw_val).strip()
        return (val_display, None, None) if val_display else (None, None, None)

    # 格式化标准日期时间
    formatted_str = dt.strftime("%Y-%m-%d %H:%M:%S")

    # 计算相对时间
    try:
        now = datetime.now()
        # 如果 dt 带时区信息，做无时区对比
        if dt.tzinfo is not None:
            dt_naive = dt.astimezone().replace(tzinfo=None)
        else:
            dt_naive = dt

        diff_sec = (now - dt_naive).total_seconds()
        days_diff = round(diff_sec / 86400.0, 1)

        if diff_sec < -60:
            relative_str = "未来"
        elif diff_sec < 60:
            relative_str = "刚刚"
        elif diff_sec < 3600:
            relative_str = f"{int(diff_sec // 60)}分钟前"
        elif diff_sec < 86400:
            relative_str = f"{round(diff_sec / 3600.0, 1)}小时前"
        elif diff_sec < 86400 * 365:
            relative_str = f"{round(diff_sec / 86400.0, 1)}天前"
        else:
            relative_str = f"{round(diff_sec / (86400.0 * 365), 1)}年前"

        return formatted_str, relative_str, days_diff
    except Exception:
        return formatted_str, None, None


@dataclass
class BindingDetail:
    """
    MC 绑定详细信息数据模型
    """
    qq: str
    player_name: str
    first_bound_time: Optional[str] = None          # 首次绑定原始值
    first_bound_formatted: Optional[str] = None     # 首次绑定标准格式化 (例如 '2026-08-25 23:05:28')
    first_bound_relative: Optional[str] = None      # 首次绑定相对时间 (例如 '1天前')
    play_count: Optional[int] = None               # 游玩次数 / 登录次数
    last_play_time: Optional[str] = None            # 上次游玩原始值
    last_play_formatted: Optional[str] = None       # 上次游玩标准格式化 (例如 '2026-08-25 23:05:28')
    last_play_relative: Optional[str] = None        # 上次游玩相对时间 (例如 '2小时前')
    updated_at: Optional[str] = None                # 账号最近更新时间
    platform: Optional[str] = "qq"                  # 平台

    def to_dict(self) -> Dict[str, Any]:
        return {
            "qq": self.qq,
            "player_name": self.player_name,
            "first_bound_time": self.first_bound_formatted or self.first_bound_time,
            "first_bound_relative": self.first_bound_relative,
            "play_count": self.play_count,
            "last_play_time": self.last_play_formatted or self.last_play_time,
            "last_play_relative": self.last_play_relative,
            "updated_at": self.updated_at,
            "platform": self.platform
        }


def get_config_val(config: Dict[str, Any], key: str, default: Any = None) -> Any:
    """
    智能读取配置项，支持顶层扁平键与嵌套模块对象
    """
    if not isinstance(config, dict):
        return default
    if key in config and config[key] is not None:
        return config[key]
    for k, v in config.items():
        if isinstance(v, dict):
            res = get_config_val(v, key, None)
            if res is not None:
                return res
    return default


def set_config_val(config: Dict[str, Any], key: str, value: Any):
    """
    智能写入配置项，优先更新嵌套模块中的对应键
    """
    if not isinstance(config, dict):
        return
    for k, v in config.items():
        if isinstance(v, dict) and key in v:
            v[key] = value
            return
    config[key] = value


class EasyBotAdapter:
    """
    EasyBot 数据源适配器
    支持从 SQLite (包括 EasyBot 2.0 EFCore 架构与 1.0 单表架构)、MySQL、JSON 文件、HTTP API 中获取 Minecraft 绑定及玩家活跃数据。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def _cfg_get(self, key: str, default: Any = None) -> Any:
        return get_config_val(self.config, key, default)

    @property
    def source_type(self) -> str:
        return self._cfg_get("data_source_type", "sqlite").lower()

    async def get_all_binding_details(self) -> Dict[str, BindingDetail]:
        """
        获取所有已绑定账号的详细数据字典
        返回: { "QQ号(str)": BindingDetail }
        """
        st = self.source_type
        try:
            if st == "sqlite":
                return await self._fetch_details_from_sqlite()
            elif st == "mysql":
                return await self._fetch_details_from_mysql()
            elif st == "json":
                return await self._fetch_details_from_json()
            elif st == "http_api":
                return await self._fetch_details_from_http_api()
            else:
                logger.error(f"[EasyBotAdapter] 未知的数据源类型: {st}")
                return {}
        except Exception as e:
            logger.error(f"[EasyBotAdapter] 从数据源 ({st}) 读取绑定详情失败: {e}", exc_info=True)
            return {}

    async def get_all_bindings(self) -> Dict[str, str]:
        """
        获取所有已绑定的 QQ -> MC 游戏名 映射字典 (向下兼容)
        返回: { "QQ号(str)": "游戏名(str)" }
        """
        details = await self.get_all_binding_details()
        return {qq: detail.player_name for qq, detail in details.items()}

    async def get_bound_qq_set(self) -> Set[str]:
        """
        获取所有已绑定 MC 账号的 QQ 号集合（去空格、统一字符串）
        """
        bindings = await self.get_all_bindings()
        return {str(k).strip() for k in bindings.keys() if str(k).strip()}

    async def get_binding_detail_by_qq(self, qq: str) -> Optional[BindingDetail]:
        """
        根据 QQ 号查询绑定的详细数据
        """
        qq_str = str(qq).strip()
        details = await self.get_all_binding_details()
        return details.get(qq_str)

    async def get_player_by_qq(self, qq: str) -> Optional[str]:
        """
        根据 QQ 号查询绑定的 MC 游戏名 (向下兼容)
        """
        detail = await self.get_binding_detail_by_qq(qq)
        return detail.player_name if detail else None

    # ---------------- SQLite 适配 ----------------
    async def _fetch_details_from_sqlite(self) -> Dict[str, BindingDetail]:
        db_path = self._cfg_get("sqlite_path", "./data/EasyBot.db")
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)

        if not os.path.exists(db_path):
            logger.warning(f"[EasyBotAdapter] SQLite 数据库文件不存在: {db_path}")
            return {}

        table = self._cfg_get("sqlite_table", "binding").strip()
        qq_col = self._cfg_get("sqlite_qq_column", "qq").strip()
        player_col = self._cfg_get("sqlite_player_column", "player_name").strip()
        first_bind_col = self._cfg_get("sqlite_first_bind_column", "").strip()
        play_count_col = self._cfg_get("sqlite_play_count_column", "").strip()
        last_play_col = self._cfg_get("sqlite_last_play_column", "").strip()

        def _sync_sqlite_fetch() -> Dict[str, BindingDetail]:
            results: Dict[str, BindingDetail] = {}
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                rows = cursor.fetchall()
                tables = [r[0] for r in rows if not r[0].startswith("sqlite_")]
                if not tables:
                    logger.warning(f"[EasyBotAdapter] SQLite 数据库 {db_path} 中无任何数据表")
                    return {}
                table_map = {t.lower(): t for t in tables}
                if "socialaccount" in table_map and "playersocialaccount" in table_map and "player" in table_map:
                    t_sa = table_map["socialaccount"]
                    t_psa = table_map["playersocialaccount"]
                    t_p = table_map["player"]
                    cursor.execute(f"PRAGMA table_info('{t_sa}')")
                    sa_cols = {c[1].lower(): c[1] for c in cursor.fetchall()}
                    cursor.execute(f"PRAGMA table_info('{t_psa}')")
                    psa_cols = {c[1].lower(): c[1] for c in cursor.fetchall()}
                    cursor.execute(f"PRAGMA table_info('{t_p}')")
                    p_cols = {c[1].lower(): c[1] for c in cursor.fetchall()}
                    sa_id_col = psa_cols.get("socialaccountsid") or psa_cols.get("socialaccountid") or list(psa_cols.values())[0]
                    user_id_col = psa_cols.get("usersid") or psa_cols.get("playersid") or psa_cols.get("playerid") or list(psa_cols.values())[1]
                    sa_created_col = sa_cols.get("createdat") or sa_cols.get("createdtime") or sa_cols.get("createtime")
                    psa_created_col = psa_cols.get("createdat") or psa_cols.get("createdtime") or psa_cols.get("createtime") or psa_cols.get("bindtime")
                    sa_updated_col = sa_cols.get("updatedat") or sa_cols.get("updatedtime") or sa_cols.get("updatetime")
                    p_play_count_col = (p_cols.get("playcount") or p_cols.get("logincount") or p_cols.get("timesplayed") or p_cols.get("gamecount") or p_cols.get("joincount") or p_cols.get("count"))
                    p_last_play_col = (p_cols.get("lastplaytime") or p_cols.get("lastplayedtime") or p_cols.get("lastlogintime") or p_cols.get("lastseen") or p_cols.get("lastjointime") or p_cols.get("lastonlinetime") or p_cols.get("updatedat"))
                    select_fields = [f"sa.{sa_cols.get('uuid', 'Uuid')} as qq", f"p.{p_cols.get('name', 'Name')} as player_name"]
                    if sa_created_col: select_fields.append(f"sa.{sa_created_col} as sa_created_at")
                    elif psa_created_col: select_fields.append(f"psa.{psa_created_col} as sa_created_at")
                    if sa_updated_col: select_fields.append(f"sa.{sa_updated_col} as sa_updated_at")
                    if p_play_count_col: select_fields.append(f"p.{p_play_count_col} as p_play_count")
                    if p_last_play_col: select_fields.append(f"p.{p_last_play_col} as p_last_play_time")
                    fields_sql = ", ".join(select_fields)
                    join_query = f"SELECT {fields_sql} FROM {t_sa} sa JOIN {t_psa} psa ON sa.Id = psa.{sa_id_col} JOIN {t_p} p ON psa.{user_id_col} = p.Id"
                    cursor.execute(join_query)
                    for r in cursor.fetchall():
                        r_dict = dict(r)
                        raw_qq = r_dict.get("qq")
                        if raw_qq is None: continue
                        qq_s = str(raw_qq).strip()
                        if not qq_s: continue
                        player_name = str(r_dict.get("player_name") or "已绑定").strip()
                        raw_bind_time = r_dict.get("sa_created_at")
                        raw_updated_at = r_dict.get("sa_updated_at")
                        raw_play_count = r_dict.get("p_play_count")
                        raw_last_play = r_dict.get("p_last_play_time")
                        fb_fmt, fb_rel, _ = parse_and_format_time(raw_bind_time)
                        lp_fmt, lp_rel, _ = parse_and_format_time(raw_last_play)
                        p_count = None
                        if raw_play_count is not None:
                            try: p_count = int(raw_play_count)
                            except (ValueError, TypeError): pass
                        up_fmt, _, _ = parse_and_format_time(raw_updated_at)
                        results[qq_s] = BindingDetail(qq=qq_s, player_name=player_name, first_bound_time=str(raw_bind_time) if raw_bind_time else None, first_bound_formatted=fb_fmt, first_bound_relative=fb_rel, play_count=p_count, last_play_time=str(raw_last_play) if raw_last_play else None, last_play_formatted=lp_fmt, last_play_relative=lp_rel, updated_at=up_fmt or (str(raw_updated_at) if raw_updated_at else None), platform="qq")
                    return results
                selected_table = None
                if table in tables: selected_table = table
                else:
                    candidate_tables = ["binding", "bindings", "whitelist", "players", "users", "easybot", "bind"]
                    for cand in candidate_tables:
                        for t in tables:
                            if cand.lower() == t.lower(): selected_table = t; break
                        if selected_table: break
                    if not selected_table and tables: selected_table = tables[0]
                cursor.execute(f"PRAGMA table_info('{selected_table}')")
                col_names = [c[1] for c in cursor.fetchall()]
                col_names_lower = {c.lower(): c for c in col_names}
                actual_qq_col = None
                if qq_col and qq_col.lower() in col_names_lower: actual_qq_col = col_names_lower[qq_col.lower()]
                else:
                    qq_candidates = ["qq", "user_id", "qq_id", "qid", "uuid", "account", "user", "qq_number", "target"]
                    for cand in qq_candidates:
                        if cand in col_names_lower: actual_qq_col = col_names_lower[cand]; break
                actual_player_col = None
                if player_col and player_col.lower() in col_names_lower: actual_player_col = col_names_lower[player_col.lower()]
                else:
                    player_candidates = ["player_name", "name", "player", "uuid", "mc_name", "ign", "game_id", "username"]
                    for cand in player_candidates:
                        if cand in col_names_lower: actual_player_col = col_names_lower[cand]; break
                actual_first_bind_col = None
                if first_bind_col and first_bind_col.lower() in col_names_lower: actual_first_bind_col = col_names_lower[first_bind_col.lower()]
                else:
                    bind_time_candidates = ["created_at", "create_time", "createdtime", "createdat", "bind_time", "bindtime", "first_bind", "time", "date", "registration_date"]
                    for cand in bind_time_candidates:
                        if cand in col_names_lower: actual_first_bind_col = col_names_lower[cand]; break
                actual_play_count_col = None
                if play_count_col and play_count_col.lower() in col_names_lower: actual_play_count_col = col_names_lower[play_count_col.lower()]
                else:
                    play_count_candidates = ["play_count", "playcount", "login_count", "logincount", "times_played", "timesplayed", "game_count", "gamecount", "play_times", "count"]
                    for cand in play_count_candidates:
                        if cand in col_names_lower: actual_play_count_col = col_names_lower[cand]; break
                actual_last_play_col = None
                if last_play_col and last_play_col.lower() in col_names_lower: actual_last_play_col = col_names_lower[last_play_col.lower()]
                else:
                    last_play_candidates = ["last_play_time", "lastplaytime", "last_played_time", "lastplayedtime", "last_login_time", "lastlogintime", "last_login", "last_seen", "lastseen", "last_join_time", "last_online_time", "updated_at", "updatedat"]
                    for cand in last_play_candidates:
                        if cand in col_names_lower: actual_last_play_col = col_names_lower[cand]; break
                if not actual_qq_col: return {}
                cols_to_select = [f"`{actual_qq_col}` as qq"]
                if actual_player_col: cols_to_select.append(f"`{actual_player_col}` as player_name")
                if actual_first_bind_col: cols_to_select.append(f"`{actual_first_bind_col}` as first_bound_time")
                if actual_play_count_col: cols_to_select.append(f"`{actual_play_count_col}` as play_count")
                if actual_last_play_col: cols_to_select.append(f"`{actual_last_play_col}` as last_play_time")
                cursor.execute(f"SELECT {', '.join(cols_to_select)} FROM `{selected_table}`")
                for row in cursor.fetchall():
                    r_dict = dict(row)
                    raw_qq = r_dict.get("qq")
                    if raw_qq is None: continue
                    qq_str = str(raw_qq).strip()
                    if not qq_str: continue
                    p_name = str(r_dict.get("player_name") or "已绑定").strip() if actual_player_col else "已绑定"
                    raw_fb = r_dict.get("first_bound_time")
                    raw_pc = r_dict.get("play_count")
                    raw_lp = r_dict.get("last_play_time")
                    fb_fmt, fb_rel, _ = parse_and_format_time(raw_fb)
                    lp_fmt, lp_rel, _ = parse_and_format_time(raw_lp)
                    pc_int = None
                    if raw_pc is not None:
                        try: pc_int = int(raw_pc)
                        except (ValueError, TypeError): pass
                    results[qq_str] = BindingDetail(qq=qq_str, player_name=p_name, first_bound_time=str(raw_fb) if raw_fb is not None else None, first_bound_formatted=fb_fmt, first_bound_relative=fb_rel, play_count=pc_int, last_play_time=str(raw_lp) if raw_lp is not None else None, last_play_formatted=lp_fmt, last_play_relative=lp_rel, platform="qq")
            finally:
                conn.close()
            return results
        bindings = await asyncio.to_thread(_sync_sqlite_fetch)
        return bindings

    # ---------------- MySQL 适配 ----------------
    async def _fetch_details_from_mysql(self) -> Dict[str, BindingDetail]:
        def _sync_fetch() -> Dict[str, BindingDetail]:
            try:
                import pymysql
            except ImportError:
                return {}
            host = self._cfg_get("mysql_host", "127.0.0.1")
            port = int(self._cfg_get("mysql_port", 3306))
            user = self._cfg_get("mysql_user", "root")
            password = self._cfg_get("mysql_password", "")
            database = self._cfg_get("mysql_database", "easybot")
            table = self._cfg_get("mysql_table", "binding")
            qq_col = self._cfg_get("mysql_qq_column", "qq")
            player_col = self._cfg_get("mysql_player_column", "player_name")
            conn = pymysql.connect(host=host, port=port, user=user, password=password, database=database, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor, connect_timeout=5)
            result: Dict[str, BindingDetail] = {}
            try:
                with conn.cursor() as cursor:
                    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
                    cols = [r["Field"] for r in cursor.fetchall()]
                    cols_lower = {c.lower(): c for c in cols}
                    actual_qq = cols_lower.get(qq_col.lower(), qq_col)
                    actual_player = cols_lower.get(player_col.lower())
                    actual_fb = cols_lower.get("created_at") or cols_lower.get("create_time") or cols_lower.get("bind_time")
                    actual_pc = cols_lower.get("play_count") or cols_lower.get("login_count") or cols_lower.get("times_played")
                    actual_lp = cols_lower.get("last_play_time") or cols_lower.get("last_played_time") or cols_lower.get("last_login_time")
                    select_list = [f"`{actual_qq}` as qq"]
                    if actual_player: select_list.append(f"`{actual_player}` as player_name")
                    if actual_fb: select_list.append(f"`{actual_fb}` as first_bound_time")
                    if actual_pc: select_list.append(f"`{actual_pc}` as play_count")
                    if actual_lp: select_list.append(f"`{actual_lp}` as last_play_time")
                    cursor.execute(f"SELECT {', '.join(select_list)} FROM `{table}`")
                    for r in cursor.fetchall():
                        raw_qq = r.get("qq")
                        if raw_qq is not None:
                            qq_str = str(raw_qq).strip()
                            if qq_str:
                                fb_fmt, fb_rel, _ = parse_and_format_time(r.get("first_bound_time"))
                                lp_fmt, lp_rel, _ = parse_and_format_time(r.get("last_play_time"))
                                pc = r.get("play_count")
                                result[qq_str] = BindingDetail(qq=qq_str, player_name=str(r.get("player_name") or "已绑定"), first_bound_time=str(r.get("first_bound_time")) if r.get("first_bound_time") else None, first_bound_formatted=fb_fmt, first_bound_relative=fb_rel, play_count=int(pc) if pc else None, last_play_time=str(r.get("last_play_time")) if r.get("last_play_time") else None, last_play_formatted=lp_fmt, last_play_relative=lp_rel, platform="qq")
            finally: conn.close()
            return result
        return await asyncio.to_thread(_sync_fetch)

    # ---------------- JSON 适配 ----------------
    async def _fetch_details_from_json(self) -> Dict[str, BindingDetail]:
        json_path = self._cfg_get("json_path", "./config/easybot_mcdr/config.json")
        if not os.path.exists(json_path): return {}
        def _read_json():
            with open(json_path, 'r', encoding='utf-8') as f: return json.load(f)
        data = await asyncio.to_thread(_read_json)
        bindings: Dict[str, BindingDetail] = {}
        if isinstance(data, dict):
            if "bindings" in data: data = data["bindings"]
            elif "users" in data: data = data["users"]
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    qq_s = str(v.get("qq") or v.get("user_id") or k).strip()
                    fb_fmt, fb_rel, _ = parse_and_format_time(v.get("first_bound_time"))
                    lp_fmt, lp_rel, _ = parse_and_format_time(v.get("last_play_time"))
                    bindings[qq_s] = BindingDetail(qq=qq_s, player_name=str(v.get("player_name") or k), first_bound_formatted=fb_fmt, first_bound_relative=fb_rel, play_count=v.get("play_count"), last_play_formatted=lp_fmt, last_play_relative=lp_rel)
                else: bindings[str(k).strip()] = BindingDetail(qq=str(k).strip(), player_name=str(v).strip())
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    qq_s = str(item.get("qq") or item.get("user_id")).strip()
                    fb_fmt, fb_rel, _ = parse_and_format_time(item.get("first_bound_time"))
                    lp_fmt, lp_rel, _ = parse_and_format_time(item.get("last_play_time"))
                    bindings[qq_s] = BindingDetail(qq=qq_s, player_name=str(item.get("player_name") or "已绑定"), first_bound_formatted=fb_fmt, first_bound_relative=fb_rel, play_count=item.get("play_count"), last_play_formatted=lp_fmt, last_play_relative=lp_rel)
        return bindings

    # ---------------- HTTP API 适配 ----------------
    async def _fetch_details_from_http_api(self) -> Dict[str, BindingDetail]:
        try:
            import httpx
        except ImportError:
            logger.error("[EasyBotAdapter] 使用 HTTP API 数据源需要安装 httpx: pip install httpx")
            return {}

        url = self._cfg_get("http_api_url", "http://127.0.0.1:8080/api/bindings")
        token = self._cfg_get("http_api_token", "").strip()

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error(f"[EasyBotAdapter] HTTP API 请求失败: {e}")
            return {}

        bindings: Dict[str, BindingDetail] = {}
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], (dict, list)):
                data = data["data"]
            elif "bindings" in data and isinstance(data["bindings"], (dict, list)):
                data = data["bindings"]

        if isinstance(data, dict):
            for k, v in data.items():
                k_str = str(k).strip()
                if isinstance(v, dict):
                    qq_val = v.get("qq") or v.get("user_id") or v.get("qid") or (k_str if k_str.isdigit() else None)
                    p_val = v.get("player_name") or v.get("name") or v.get("player") or v.get("uuid") or "已绑定"
                    raw_fb = v.get("first_bound_time") or v.get("created_at") or v.get("bind_time")
                    raw_pc = v.get("play_count") or v.get("login_count") or v.get("times_played")
                    raw_lp = v.get("last_play_time") or v.get("last_played_time") or v.get("last_login_time")

                    if qq_val:
                        qq_s = str(qq_val).strip()
                        fb_fmt, fb_rel, _ = parse_and_format_time(raw_fb)
                        lp_fmt, lp_rel, _ = parse_and_format_time(raw_lp)
                        pc_int = int(raw_pc) if raw_pc is not None and str(raw_pc).isdigit() else None
                        bindings[qq_s] = BindingDetail(
                            qq=qq_s,
                            player_name=str(p_val).strip(),
                            first_bound_time=str(raw_fb) if raw_fb else None,
                            first_bound_formatted=fb_fmt,
                            first_bound_relative=fb_rel,
                            play_count=pc_int,
                            last_play_time=str(raw_lp) if raw_lp else None,
                            last_play_formatted=lp_fmt,
                            last_play_relative=lp_rel,
                            platform="qq"
                        )
                else:
                    v_str = str(v).strip()
                    if k_str.isdigit() and len(k_str) >= 5:
                        bindings[k_str] = BindingDetail(qq=k_str, player_name=v_str)
                    elif v_str.isdigit() and len(v_str) >= 5:
                        bindings[v_str] = BindingDetail(qq=v_str, player_name=k_str)

        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    qq_val = item.get("qq") or item.get("user_id") or item.get("qq_id") or item.get("qid")
                    p_val = item.get("player_name") or item.get("name") or item.get("player") or item.get("uuid") or "已绑定"
                    raw_fb = item.get("first_bound_time") or item.get("created_at") or item.get("bind_time")
                    raw_pc = item.get("play_count") or item.get("login_count") or item.get("times_played")
                    raw_lp = item.get("last_play_time") or item.get("last_played_time") or item.get("last_login_time")

                    if qq_val:
                        qq_s = str(qq_val).strip()
                        fb_fmt, fb_rel, _ = parse_and_format_time(raw_fb)
                        lp_fmt, lp_rel, _ = parse_and_format_time(raw_lp)
                        pc_int = int(raw_pc) if raw_pc is not None and str(raw_pc).isdigit() else None
                        bindings[qq_s] = BindingDetail(
                            qq=qq_s,
                            player_name=str(p_val).strip(),
                            first_bound_time=str(raw_fb) if raw_fb else None,
                            first_bound_formatted=fb_fmt,
                            first_bound_relative=fb_rel,
                            play_count=pc_int,
                            last_play_time=str(raw_lp) if raw_lp else None,
                            last_play_formatted=lp_fmt,
                            last_play_relative=lp_rel,
                            platform="qq"
                        )
                elif isinstance(item, (int, str)):
                    qq_s = str(item).strip()
                    bindings[qq_s] = BindingDetail(qq=qq_s, player_name="已绑定")

        logger.info(f"[EasyBotAdapter] 成功从 HTTP API 读取到 {len(bindings)} 条绑定详细数据")
        return bindings
