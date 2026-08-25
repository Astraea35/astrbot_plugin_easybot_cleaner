import os
import sys
import time
import json
import sqlite3
import asyncio
import unittest

# 将项目目录加入 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from easybot_adapter import EasyBotAdapter
from member_checker import MemberChecker, KickManager, InactiveMemberInfo

class TestEasyBotCleaner(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.test_dir = os.path.dirname(__file__)
        self.db_path = os.path.join(self.test_dir, "test_easybot.db")
        self.json_path = os.path.join(self.test_dir, "test_easybot.json")

        # 1. 创建测试 SQLite 数据库
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE binding (qq TEXT PRIMARY KEY, player_name TEXT)")
        cursor.execute("INSERT INTO binding VALUES ('10001', 'Alex')")
        cursor.execute("INSERT INTO binding VALUES ('10002', 'Steve')")
        conn.commit()
        conn.close()

        # 2. 创建测试 JSON 文件
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump({"20001": "Notch", "20002": "Jeb_"}, f)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        if os.path.exists(self.json_path):
            os.remove(self.json_path)

    async def test_sqlite_adapter(self):
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

    async def test_json_adapter(self):
        adapter = EasyBotAdapter({
            "data_source_type": "json",
            "json_path": self.json_path
        })
        bound_set = await adapter.get_bound_qq_set()
        self.assertIn("20001", bound_set)
        self.assertIn("20002", bound_set)

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
            # 1. 群主 (豁免)
            {"user_id": 1, "nickname": "群主", "role": "owner", "join_time": now - 86400*100, "last_sent_time": 0},
            # 2. 管理员 (豁免)
            {"user_id": 2, "nickname": "管理员", "role": "admin", "join_time": now - 86400*100, "last_sent_time": 0},
            # 3. 机器人自身 (豁免)
            {"user_id": 3, "nickname": "Bot", "role": "member", "join_time": now - 86400*100, "last_sent_time": 0},
            # 4. 白名单成员 (豁免)
            {"user_id": 88888, "nickname": "特权大佬", "role": "member", "join_time": now - 86400*100, "last_sent_time": 0},
            # 5. 新人保护期内（进群 2 天，未绑定未发言，应豁免）
            {"user_id": 4, "nickname": "新入群萌新", "role": "member", "join_time": now - 86400*2, "last_sent_time": 0},
            # 6. 已绑定 MC 玩家（哪怕 60 天没发言也应豁免）
            {"user_id": 10001, "nickname": "MC老玩家", "role": "member", "join_time": now - 86400*100, "last_sent_time": now - 86400*60},
            # 7. 未绑定，但昨天刚发言 (应豁免)
            {"user_id": 5, "nickname": "活跃潜水员", "role": "member", "join_time": now - 86400*100, "last_sent_time": now - 86400*1},
            # 8. 未绑定，最后发言在 45 天前 (目标待清理成员！)
            {"user_id": 6, "nickname": "长期潜水A", "card": "小六", "role": "member", "join_time": now - 86400*100, "last_sent_time": now - 86400*45},
            # 9. 未绑定，入群 50 天从未发言 (目标待清理成员！)
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

if __name__ == "__main__":
    unittest.main()
