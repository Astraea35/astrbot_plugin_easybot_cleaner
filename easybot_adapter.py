import os
import json
import sqlite3
import logging
import asyncio
from typing import Dict, Set, Optional, Tuple, Any

logger = logging.getLogger("astrbot")

class EasyBotAdapter:
    """
    EasyBot 数据源适配器
    支持从 SQLite、MySQL、JSON 文件、HTTP API 中获取 Minecraft 绑定数据。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @property
    def source_type(self) -> str:
        return self.config.get("data_source_type", "sqlite").lower()

    async def get_all_bindings(self) -> Dict[str, str]:
        """
        获取所有已绑定的 QQ -> MC 游戏名 映射字典
        返回: { "QQ号(str)": "游戏名(str)" }
        """
        st = self.source_type
        try:
            if st == "sqlite":
                return await self._fetch_from_sqlite()
            elif st == "mysql":
                return await self._fetch_from_mysql()
            elif st == "json":
                return await self._fetch_from_json()
            elif st == "http_api":
                return await self._fetch_from_http_api()
            else:
                logger.error(f"[EasyBotAdapter] 未知的数据源类型: {st}")
                return {}
        except Exception as e:
            logger.error(f"[EasyBotAdapter] 从数据源 ({st}) 读取绑定数据失败: {e}", exc_info=True)
            return {}

    async def get_bound_qq_set(self) -> Set[str]:
        """
        获取所有已绑定 MC 账号的 QQ 号集合（去空格、统一字符串）
        """
        bindings = await self.get_all_bindings()
        return {str(k).strip() for k in bindings.keys() if str(k).strip()}

    async def get_player_by_qq(self, qq: str) -> Optional[str]:
        """
        根据 QQ 号查询绑定的 MC 游戏名
        """
        qq_str = str(qq).strip()
        bindings = await self.get_all_bindings()
        return bindings.get(qq_str)

    # ---------------- SQLite 适配 ----------------
    async def _fetch_from_sqlite(self) -> Dict[str, str]:
        db_path = self.config.get("sqlite_path", "./data/EasyBot.db")
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)

        if not os.path.exists(db_path):
            logger.warning(f"[EasyBotAdapter] SQLite 数据库文件不存在: {db_path}")
            return {}

        table = self.config.get("sqlite_table", "binding").strip()
        qq_col = self.config.get("sqlite_qq_column", "qq").strip()
        player_col = self.config.get("sqlite_player_column", "player_name").strip()

        def _sync_sqlite_fetch() -> Dict[str, str]:
            bindings: Dict[str, str] = {}
            # 使用 URI 模式只读打开，避免占用锁
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.cursor()

                # 探测所有表名
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                rows = cursor.fetchall()
                tables = [r[0] for r in rows if not r[0].startswith("sqlite_")]

                if not tables:
                    logger.warning(f"[EasyBotAdapter] SQLite 数据库 {db_path} 中无任何数据表")
                    return {}

                selected_table = None
                if table in tables:
                    selected_table = table
                else:
                    # 尝试模糊匹配常见表名
                    candidate_tables = ["binding", "bindings", "whitelist", "players", "users", "easybot", "bind"]
                    for cand in candidate_tables:
                        for t in tables:
                            if cand.lower() == t.lower():
                                selected_table = t
                                break
                        if selected_table:
                            break
                    if not selected_table and tables:
                        selected_table = tables[0]

                logger.info(f"[EasyBotAdapter] 使用 SQLite 数据表: {selected_table}")

                # 探测列名
                cursor.execute(f"PRAGMA table_info('{selected_table}')")
                cols_info = cursor.fetchall()
                col_names = [c[1] for c in cols_info]

                # 确定 QQ 列
                actual_qq_col = None
                if qq_col in col_names:
                    actual_qq_col = qq_col
                else:
                    qq_candidates = ["qq", "user_id", "qq_id", "qid", "account", "user", "qq_number", "target"]
                    for cand in qq_candidates:
                        for c in col_names:
                            if cand.lower() == c.lower():
                                actual_qq_col = c
                                break
                        if actual_qq_col:
                            break

                # 确定 Player 列
                actual_player_col = None
                if player_col in col_names:
                    actual_player_col = player_col
                else:
                    player_candidates = ["player_name", "name", "player", "uuid", "mc_name", "ign", "game_id", "username"]
                    for cand in player_candidates:
                        for c in col_names:
                            if cand.lower() == c.lower():
                                actual_player_col = c
                                break
                        if actual_player_col:
                            break

                if not actual_qq_col:
                    logger.error(f"[EasyBotAdapter] 无法在表 {selected_table} 中识别 QQ 字段，现有字段: {col_names}")
                    return {}

                query = f"SELECT {actual_qq_col}" + (f", {actual_player_col}" if actual_player_col else "") + f" FROM {selected_table}"
                cursor.execute(query)
                for row in cursor.fetchall():
                    raw_qq = row[actual_qq_col]
                    if raw_qq is None:
                        continue
                    qq_str = str(raw_qq).strip()
                    if not qq_str:
                        continue
                    player_name = str(row[actual_player_col]).strip() if actual_player_col and row[actual_player_col] else "已绑定"
                    bindings[qq_str] = player_name
            finally:
                conn.close()

            return bindings

        bindings = await asyncio.to_thread(_sync_sqlite_fetch)
        logger.info(f"[EasyBotAdapter] 成功从 SQLite 读取到 {len(bindings)} 条绑定数据")
        return bindings

    # ---------------- MySQL 适配 ----------------
    async def _fetch_from_mysql(self) -> Dict[str, str]:
        def _sync_fetch():
            try:
                import pymysql
            except ImportError:
                logger.error("[EasyBotAdapter] 使用 MySQL 数据源需要安装 pymysql: pip install pymysql")
                return {}

            host = self.config.get("mysql_host", "127.0.0.1")
            port = int(self.config.get("mysql_port", 3306))
            user = self.config.get("mysql_user", "root")
            password = self.config.get("mysql_password", "")
            database = self.config.get("mysql_database", "easybot")
            table = self.config.get("mysql_table", "binding")
            qq_col = self.config.get("mysql_qq_column", "qq")
            player_col = self.config.get("mysql_player_column", "player_name")

            conn = pymysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=5
            )
            result = {}
            try:
                with conn.cursor() as cursor:
                    sql = f"SELECT `{qq_col}`, `{player_col}` FROM `{table}`"
                    cursor.execute(sql)
                    rows = cursor.fetchall()
                    for r in rows:
                        raw_qq = r.get(qq_col)
                        if raw_qq is not None:
                            qq_str = str(raw_qq).strip()
                            if qq_str:
                                p_name = str(r.get(player_col, "已绑定")).strip()
                                result[qq_str] = p_name
            finally:
                conn.close()
            return result

        bindings = await asyncio.to_thread(_sync_fetch)
        logger.info(f"[EasyBotAdapter] 成功从 MySQL 读取到 {len(bindings)} 条绑定数据")
        return bindings

    # ---------------- JSON 适配 ----------------
    async def _fetch_from_json(self) -> Dict[str, str]:
        json_path = self.config.get("json_path", "./config/easybot_mcdr/config.json")
        if not os.path.isabs(json_path):
            json_path = os.path.abspath(json_path)

        if not os.path.exists(json_path):
            logger.warning(f"[EasyBotAdapter] JSON 绑定文件不存在: {json_path}")
            return {}

        def _read_json():
            with open(json_path, 'r', encoding='utf-8') as f:
                return json.load(f)

        data = await asyncio.to_thread(_read_json)
        bindings: Dict[str, str] = {}

        # 智能解析各种 JSON 结构
        if isinstance(data, dict):
            if "bindings" in data and isinstance(data["bindings"], dict):
                data = data["bindings"]
            elif "users" in data and isinstance(data["users"], list):
                data = data["users"]

        if isinstance(data, dict):
            for k, v in data.items():
                k_str = str(k).strip()
                v_str = str(v).strip()
                if k_str.isdigit() and len(k_str) >= 5:
                    bindings[k_str] = v_str
                elif v_str.isdigit() and len(v_str) >= 5:
                    bindings[v_str] = k_str
                elif isinstance(v, dict):
                    qq_val = v.get("qq") or v.get("user_id") or v.get("qid")
                    if qq_val:
                        bindings[str(qq_val).strip()] = k_str
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    qq_val = item.get("qq") or item.get("user_id") or item.get("qq_id") or item.get("qid")
                    p_val = item.get("player_name") or item.get("name") or item.get("player") or item.get("uuid") or "已绑定"
                    if qq_val:
                        bindings[str(qq_val).strip()] = str(p_val).strip()

        logger.info(f"[EasyBotAdapter] 成功从 JSON 文件读取到 {len(bindings)} 条绑定数据")
        return bindings

    # ---------------- HTTP API 适配 ----------------
    async def _fetch_from_http_api(self) -> Dict[str, str]:
        try:
            import httpx
        except ImportError:
            logger.error("[EasyBotAdapter] 使用 HTTP API 数据源需要安装 httpx: pip install httpx")
            return {}

        url = self.config.get("http_api_url", "http://127.0.0.1:8080/api/bindings")
        token = self.config.get("http_api_token", "").strip()

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        bindings: Dict[str, str] = {}
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], (dict, list)):
                data = data["data"]
            elif "bindings" in data and isinstance(data["bindings"], (dict, list)):
                data = data["bindings"]

        if isinstance(data, dict):
            for k, v in data.items():
                k_str = str(k).strip()
                v_str = str(v).strip()
                if k_str.isdigit() and len(k_str) >= 5:
                    bindings[k_str] = v_str
                elif v_str.isdigit() and len(v_str) >= 5:
                    bindings[v_str] = k_str
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    qq_val = item.get("qq") or item.get("user_id") or item.get("qq_id")
                    p_val = item.get("player_name") or item.get("name") or item.get("player") or "已绑定"
                    if qq_val:
                        bindings[str(qq_val).strip()] = str(p_val).strip()
                elif isinstance(item, (int, str)):
                    bindings[str(item).strip()] = "已绑定"

        logger.info(f"[EasyBotAdapter] 成功从 HTTP API 读取到 {len(bindings)} 条绑定数据")
        return bindings
