import os
import sys
import time
import json
import sqlite3
import asyncio
import unittest
from datetime import datetime

# 将项目目录加入 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from easybot_adapter import EasyBotAdapter, BindingDetail, parse_and_format_time
from member_checker import MemberChecker, KickManager, InactiveMemberInfo

class TestEasyBotCleaner(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.test_dir = os.path.dirname(__file__)
        self.db_path = os.path.join(self.test_dir, "test_easybot.db")
        self.db2_path = os.path.join(self.test_dir, "test_easybot2.db")
        self.json_path = os.path.join(self.test_dir, "test_easybot.json")

        # 1. 创建单表测试 SQLite 数据库 (包含扩展字段)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE binding (
            qq TEXT PRIMARY KEY, 
            player_name TEXT, 
            created_at TEXT, 
            play_count INTEGER, 
            last_play_time TEXT
        )
        """)
        cursor.execute("INSERT INTO binding VALUES ('10001', 'Alex', '2026-08-20 12:00:00', 10, '2026-08-25 18:30:00')")
        cursor.execute("INSERT INTO binding VALUES ('10002', 'Steve', '2026-08-21 15:00:00', 5, '2026-08-24 10:00:00')")
        conn.commit()
        conn.close()

        # 2. 创建 EasyBot 2.0 架构测试 SQLite 数据库 (SocialAccount + PlayerSocialAccount + Player)
        if os.path.exists(self.db2_path):
            os.remove(self.db2_path)
        conn2 = sqlite3.connect(self.db2_path)
        c2 = conn2.cursor()
        c2.execute("""
        CREATE TABLE SocialAccount (
            Id INTEGER PRIMARY KEY,
            Platform TEXT,
            Name TEXT,
            Uuid TEXT,
            CreatedAt TEXT,
            UpdatedAt TEXT
        )
        """)
        c2.execute("""
        CREATE TABLE Player (
            Id INTEGER PRIMARY KEY,
            Name TEXT,
            CreatedAt TEXT,
            PlayCount INTEGER,
            LastPlayTime TEXT
        )
        """)
        c2.execute("""
        CREATE TABLE PlayerSocialAccount (
            SocialAccountsId INTEGER,
            UsersId INTEGER,
            PRIMARY KEY (SocialAccountsId, UsersId)
        )
        """)
        # 插入模拟 EasyBot 2.0 数据 (注意 ISO 格式带7位微秒)
        c2.execute("INSERT INTO SocialAccount VALUES (1, 'qq', '溯', '2846924897', '2026-08-25T23:05:28.3503607', '2026-08-25T23:05:28.3503697')")
        c2.execute("INSERT INTO Player VALUES (101, 'Su_Player', '2026-08-25T23:05:28.3503607', 15, '2026-08-25T23:50:00.0000000')")
        c2.execute("INSERT INTO PlayerSocialAccount VALUES (1, 101)")
        conn2.commit()
        conn2.close()

        # 3. 创建测试 JSON 文件 (支持丰富字段与字典结构)
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump({
                "20001": {
                    "player_name": "Notch",
                    "first_bound_time": "2026-08-22 10:00:00",
                    "play_count": 42,
                    "last_play_time": "2026-08-25 20:00:00"
                },
                "20002": "Jeb_"
            }, f)

    def tearDown(self):
        for p in [self.db_path, self.db2_path, self.json_path]:
            if os.path.exists(p):
                os.remove(p)

    def test_parse_and_format_time(self):
        # 1. 测试 EasyBot 2.0 典型 ISO 格式（含7位微秒）
        iso_str = "2026-08-25T23:05:28.3503607"
        fmt, rel, days = parse_and_format_time(iso_str)
        self.assertEqual(fmt, "2026-08-25 23:05:28")
        self.assertIsNotNone(rel)

        # 2. 测试标准 SQL 日期格式
        sql_str = "2026-08-25 12:00:00"
        fmt, rel, days = parse_and_format_time(sql_str)
        self.assertEqual(fmt, "2026-08-25 12:00:00")

        # 3. 测试 Unix 时间戳 (秒级)
        ts_sec = 1787710566
        fmt, rel, days = parse_and_format_time(ts_sec)
        self.assertIsNotNone(fmt)

        # 4. 测试 Unix 时间戳 (毫秒级)
        ts_ms = 1787710566000
        fmt, rel, days = parse_and_format_time(ts_ms)
        self.assertIsNotNone(fmt)

        # 5. 测试空值容错
        fmt, rel, days = parse_and_format_time(None)
        self.assertIsNone(fmt)
        fmt, rel, days = parse_and_format_time("")
        self.assertIsNone(fmt)
        fmt, rel, days = parse_and_format_time("null")
        self.assertIsNone(fmt)

    async def test_sqlite_single_table_adapter(self):
        adapter = EasyBotAdapter({
            "data_source_type": "sqlite",
            "sqlite_path": self.db_path,
            "sqlite_table": "binding",
            "sqlite_qq_column": "qq",
            "sqlite_player_column": "player_name"
        })
        bound_set = await adapter.get_bound_qq_set()
        self.assertIn("10001", bound_set)
        self.assertIn("10002", bound_set)
        self.assertNotIn("99999", bound_set)

        p1 = await adapter.get_player_by_qq("10001")
        self.assertEqual(p1, "Alex")

        detail = await adapter.get_binding_detail_by_qq("10001")
        self.assertIsNotNone(detail)
        self.assertEqual(detail.qq, "10001")
        self.assertEqual(detail.player_name, "Alex")
        self.assertEqual(detail.first_bound_formatted, "2026-08-20 12:00:00")
        self.assertEqual(detail.play_count, 10)
        self.assertEqual(detail.last_play_formatted, "2026-08-25 18:30:00")

    async def test_sqlite_easybot2_adapter(self):
        adapter = EasyBotAdapter({
            "data_source_type": "sqlite",
            "sqlite_path": self.db2_path
        })
        bound_set = await adapter.get_bound_qq_set()
        self.assertIn("2846924897", bound_set)

        detail = await adapter.get_binding_detail_by_qq("2846924897")
        self.assertIsNotNone(detail)
        self.assertEqual(detail.qq, "2846924897")
        self.assertEqual(detail.player_name, "Su_Player")
        self.assertEqual(detail.first_bound_formatted, "2026-08-25 23:05:28")
        self.assertEqual(detail.play_count, 15)
        self.assertEqual(detail.last_play_formatted, "2026-08-25 23:50:00")
        self.assertIsNotNone(detail.first_bound_relative)

    async def test_json_adapter(self):
        adapter = EasyBotAdapter({
            "data_source_type": "json",
            "json_path": self.json_path
        })
        bound_set = await adapter.get_bound_qq_set()
        self.assertIn("20001", bound_set)
        self.assertIn("20002", bound_set)

        detail = await adapter.get_binding_detail_by_qq("20001")
        self.assertIsNotNone(detail)
        self.assertEqual(detail.player_name, "Notch")
        self.assertEqual(detail.first_bound_formatted, "2026-08-22 10:00:00")
        self.assertEqual(detail.play_count, 42)
        self.assertEqual(detail.last_play_formatted, "2026-08-25 20:00:00")

    def test_member_filter(self):
        config = {
            "default_inactive_days": 30,
            "new_member_grace_days": 3,
            "whitelist_qqs": ["88888"]
        }
        checker = MemberChecker(config)
        now = int(time.time())

        # 模拟各种群成员数据
        members = [
            {"user_id": 1, "nickname": "群主", "role": "owner", "join_time": now - 86400*100, "last_sent_time": 0},
            {"user_id": 2, "nickname": "管理员", "role": "admin", "join_time": now - 86400*100, "last_sent_time": 0},
            {"user_id": 3, "nickname": "Bot", "role": "member", "join_time": now - 86400*100, "last_sent_time": 0},
            {"user_id": 88888, "nickname": "特权大佬", "role": "member", "join_time": now - 86400*100, "last_sent_time": 0},
            {"user_id": 4, "nickname": "新入群萌新", "role": "member", "join_time": now - 86400*2, "last_sent_time": 0},
            {"user_id": 10001, "nickname": "MC老玩家", "role": "member", "join_time": now - 86400*100, "last_sent_time": now - 86400*60},
            {"user_id": 5, "nickname": "活跃潜水员", "role": "member", "join_time": now - 86400*100, "last_sent_time": now - 86400*1},
            {"user_id": 6, "nickname": "长期潜水A", "card": "小六", "role": "member", "join_time": now - 86400*100, "last_sent_time": now - 86400*45},
            {"user_id": 7, "nickname": "从不发言B", "role": "member", "join_time": now - 86400*50, "last_sent_time": 0},
        ]

        bound_set = {"10001", "10002"}
        report = checker.filter_inactive_members(
            group_id="12345678",
            member_list=members,
            bound_qq_set=bound_set,
            bot_self_id="3",
            override_inactive_days=30
        )

        self.assertEqual(report.total_members, 9)
        self.assertEqual(report.bound_count, 1)    # 10001
        self.assertEqual(report.exempt_count, 4)   # 群主(1), 管理员(2), Bot(3), 白名单(88888)
        self.assertEqual(report.grace_count, 1)    # 新人(4)
        self.assertEqual(report.active_unbound_count, 1) # 活跃(5)

        candidate_ids = [c.user_id for c in report.inactive_candidates]
        self.assertEqual(len(candidate_ids), 2)
        self.assertIn("6", candidate_ids)
        self.assertIn("7", candidate_ids)

    def test_mc_inactive_filter_enabled(self):
        """测试开启 MC 长期未上线自动清理时的过滤行为"""
        now = int(time.time())
        config = {
            "default_inactive_days": 30,
            "mc_inactive_clean_enabled": True,
            "mc_inactive_days": 30,
            "new_member_grace_days": 3,
            "whitelist_qqs": ["88888"]
        }
        checker = MemberChecker(config)

        # 成员列表
        members = [
            # 1. 已绑定，但游戏内 45 天未上线 (应被标记为待清理！)
            {"user_id": 10001, "nickname": "MC老玩家", "role": "member", "join_time": now - 86400*100, "last_sent_time": now - 86400*1},
            # 2. 已绑定，游戏内 5 天前刚上线 (应豁免)
            {"user_id": 10002, "nickname": "MC活跃玩家", "role": "member", "join_time": now - 86400*100, "last_sent_time": now - 86400*1},
            # 3. 白名单，哪怕 100 天没上线也应豁免
            {"user_id": 88888, "nickname": "特权大佬", "role": "member", "join_time": now - 86400*100, "last_sent_time": 0},
            # 4. 未绑定，40 天未发言 (应被标记为待清理！)
            {"user_id": 10003, "nickname": "未绑定潜水员", "role": "member", "join_time": now - 86400*100, "last_sent_time": now - 86400*40},
        ]

        # 绑定详情映射 (带最后游玩时间)
        last_play_45d_ago = datetime.fromtimestamp(now - 86400*45).strftime("%Y-%m-%d %H:%M:%S")
        last_play_5d_ago = datetime.fromtimestamp(now - 86400*5).strftime("%Y-%m-%d %H:%M:%S")
        last_play_100d_ago = datetime.fromtimestamp(now - 86400*100).strftime("%Y-%m-%d %H:%M:%S")

        bound_details = {
            "10001": BindingDetail(qq="10001", player_name="OldGuy", last_play_time=last_play_45d_ago),
            "10002": BindingDetail(qq="10002", player_name="ActiveGuy", last_play_time=last_play_5d_ago),
            "88888": BindingDetail(qq="88888", player_name="VIP", last_play_time=last_play_100d_ago),
        }

        report = checker.filter_inactive_members(
            group_id="123456",
            member_list=members,
            bound_qq_set=bound_details,
            override_inactive_days=30
        )

        self.assertEqual(report.total_members, 4)
        self.assertEqual(report.bound_count, 1)        # 10002 (ActiveGuy)
        self.assertEqual(report.exempt_count, 1)       # 88888 (Whitelist)
        self.assertEqual(report.mc_inactive_count, 1)  # 10001 (OldGuy, 45d inactive)
        self.assertEqual(len(report.inactive_candidates), 2) # 10001 (MC未上线) + 10003 (未绑定未发言)

        c_map = {c.user_id: c for c in report.inactive_candidates}
        self.assertIn("10001", c_map)
        self.assertIn("10003", c_map)
        self.assertEqual(c_map["10001"].member_type, "mc_inactive")
        self.assertEqual(c_map["10003"].member_type, "unbound")

    def test_whitelist_helpers(self):
        config = {"whitelist_qqs": ["111", "222"]}
        checker = MemberChecker(config)
        wl = checker.get_whitelist_set()
        self.assertIn("111", wl)
        self.assertIn("222", wl)
        self.assertNotIn("333", wl)

    async def test_kick_batch(self):
        class MockBot:
            def __init__(self):
                self.kicked = []

            class API:
                def __init__(self, outer):
                    self.outer = outer
                async def call_action(self, action, **kwargs):
                    if action == "set_group_kick":
                        self.outer.kicked.append((kwargs["group_id"], kwargs["user_id"]))
                        return {"status": "ok"}

            def __init__(self):
                self.kicked = []
                self.api = self.API(self)

        bot = MockBot()
        mgr = KickManager({"kick_interval_seconds": 0.01})
        candidates = [
            InactiveMemberInfo(
                user_id="1001",
                nickname="User1",
                card="",
                role="member",
                join_time=0,
                last_sent_time=0,
                days_since_last_sent=None,
                days_since_join=50.0,
                reason="未绑定MC"
            ),
            InactiveMemberInfo(
                user_id="1002",
                nickname="User2",
                card="Card2",
                role="member",
                join_time=0,
                last_sent_time=0,
                days_since_last_sent=None,
                days_since_join=40.0,
                reason="未绑定MC"
            )
        ]

        success, failed = await mgr.execute_kick_batch(bot, "123456", candidates, interval_seconds=0.01)
        self.assertEqual(len(success), 2)
        self.assertEqual(len(failed), 0)
        self.assertEqual(len(bot.kicked), 2)
        self.assertEqual(bot.kicked[0], (123456, 1001))
        self.assertEqual(bot.kicked[1], (123456, 1002))

    def test_modular_config_support(self):
        """测试按模块划分的嵌套配置能否被正确读取与写入"""
        from easybot_adapter import get_config_val, set_config_val
        
        modular_config = {
            "data_source": {
                "data_source_type": "sqlite",
                "sqlite_path": "./data/EasyBot.db",
                "sqlite_table": "binding"
            },
            "clean_rules": {
                "default_inactive_days": 15,
                "new_member_grace_days": 5,
                "whitelist_qqs": ["111", "222"],
                "target_groups": ["999"],
                "kick_interval_seconds": 2.0
            },
            "mc_inactive_clean": {
                "mc_inactive_clean_enabled": True,
                "mc_inactive_days": 20
            },
            "auto_clean_schedule": {
                "auto_clean_enabled": True,
                "auto_clean_hour": 3,
                "auto_clean_minute": 30,
                "auto_clean_mode": "execute_kick"
            }
        }

        # 1. 验证 get_config_val
        self.assertEqual(get_config_val(modular_config, "data_source_type"), "sqlite")
        self.assertEqual(get_config_val(modular_config, "default_inactive_days"), 15)
        self.assertEqual(get_config_val(modular_config, "mc_inactive_clean_enabled"), True)
        self.assertEqual(get_config_val(modular_config, "auto_clean_hour"), 3)
        self.assertEqual(get_config_val(modular_config, "non_existent_key", 999), 999)

        # 2. 验证 set_config_val
        set_config_val(modular_config, "whitelist_qqs", ["111", "222", "333"])
        self.assertEqual(modular_config["clean_rules"]["whitelist_qqs"], ["111", "222", "333"])

        # 3. 验证 MemberChecker 配合模块化配置
        checker = MemberChecker(modular_config)
        self.assertIn("333", checker.get_whitelist_set())
        self.assertEqual(checker._cfg_get("mc_inactive_days"), 20)

if __name__ == "__main__":
    unittest.main()

