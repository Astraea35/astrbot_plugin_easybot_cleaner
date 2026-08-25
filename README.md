# AstrBot 插件：EasyBot 联动 MC 绑定与潜水清理 (astrbot_plugin_easybot_cleaner)

一款专为 Minecraft（我的世界）服务器群设计的 AstrBot 插件。通过与 **EasyBot**（支持 SQLite、MySQL、JSON 文件或 HTTP API）深度联动，精准查询群成员的 MC 游戏账号绑定状态，自动豁免已绑定玩家，并对未绑定且超出规定天数未发言的潜水成员进行扫描识别与批量自动清理。

---

## ✨ 核心特性

- 🔗 **多数据源 EasyBot 联动**：
  - **SQLite 本地数据库**：直连 EasyBot 的 `.db` 数据库（如 `EasyBot.db`），自动探测表名与字段。
  - **MySQL 远程数据库**：支持多服/跨服 MySQL 数据库直连查询。
  - **JSON 绑定文件**：支持读取 EasyBot-MCDR 配置文件或自定义 JSON 映射文件。
  - **HTTP REST API**：支持通过远程 API 接口实时获取绑定名单。
- 🛡️ **全方位安全防护与智能豁免**：
  - **管理员与群主豁免**：自动跳过群主、群管理员及机器人自身。
  - **永久白名单**：支持配置或动态添加特定 QQ 白名单，永久豁免清理。
  - **新人缓冲保护期**：支持设置新人入群保护天数（默认 3 天），防止刚进群的新人未来得及绑定被误踢。
- ⚡ **安全预览与防风控执行**：
  - **扫描预览模式（Dry-Run）**：一键生成详尽的潜水报告与名单，不执行实际踢人。
  - **二次确认机制**：执行清理指令需要二次确认或添加 `confirm` 参数，防止误触。
  - **防风控频率限制**：批量踢人内置异步延时间隔（默认 1.5s/人），保障机器人账号安全。
- ⏰ **定时自动巡检**：支持设置每日定时任务，自动扫描群成员并发送通知或执行清理。

---

## 📦 安装与部署

### 1. 放入插件目录
将本插件文件夹复制到 AstrBot 项目的插件目录下：
```text
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_easybot_cleaner/
            ├── metadata.yaml
            ├── _conf_schema.json
            ├── requirements.txt
            ├── main.py
            ├── easybot_adapter.py
            ├── member_checker.py
            └── README.md
```

### 2. 安装 Python 依赖
在 AstrBot 的 Python 环境中安装依赖：
```bash
pip install -r requirements.txt
```
*(依赖包含: `aiosqlite`, `pymysql`, `httpx`)*

### 3. 重载或重启 AstrBot
在 AstrBot Web 控制台的「插件管理」页面点击 **重载插件**，或重启 AstrBot 即可。

---

## ⚙️ 配置说明

在 AstrBot Web 管理界面的插件配置中，可进行可视化配置：

### 1. 数据源配置（根据你的 EasyBot 部署方式选择）

#### 方式 A：SQLite 数据库（最常见）
- **数据源类型 (`data_source_type`)**：`sqlite`
- **数据库路径 (`sqlite_path`)**：填写 EasyBot 生成的 SQLite 文件路径，例如：
  - Windows: `C:/MinecraftServer/plugins/EasyBot/EasyBot.db`
  - Linux: `/opt/mcserver/plugins/EasyBot/EasyBot.db`
- **表名与字段**：插件默认已内置智能探测（支持 `binding`、`whitelist`、`players` 等表名和 `qq`、`user_id` 等字段），通常保持默认即可。

#### 方式 B：MySQL 数据库
- **数据源类型 (`data_source_type`)**：`mysql`
- **MySQL 主机/端口/账号/密码/数据库名**：按需填写。

#### 方式 C：JSON 文件
- **数据源类型 (`data_source_type`)**：`json`
- **JSON 路径 (`json_path`)**：例如 `./config/easybot_mcdr/config.json`。

### 2. 清理规则配置
| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `default_inactive_days` | `30` | 默认未发言天数阈值（未绑定且超过该天数未发言即判定为潜水） |
| `new_member_grace_days` | `3` | 新人入群保护天数（入群未满 3 天即使未绑定也不踢） |
| `whitelist_qqs` | `[]` | 永久豁免 QQ 列表 |
| `target_groups` | `[]` | 允许生效的群号列表（留空表示所有群） |
| `kick_interval_seconds` | `1.5` | 批量踢人时的每个请求间隔（建议 1.0~3.0 秒防风控） |
| `auto_clean_enabled` | `false` | 是否开启每日定时巡检 |
| `auto_clean_hour` | `4` | 定时巡检执行小时（0~23） |
| `auto_clean_mode` | `notify_only` | `notify_only`（仅群内报警）或 `execute_kick`（自动踢出） |

---

## 🎮 指令使用手册

> 💡 **提示**：管理类指令（`/mc_scan`、`/mc_clean`、`/mc_whitelist`）仅限**群主**、**群管理员**或**机器人管理员**使用。

### 1. 扫描与预览 `/mc_scan`
```text
/mc_scan        # 使用默认配置的天数（如 30 天）进行扫描
/mc_scan 15     # 扫描未绑定且超过 15 天未发言的成员
```
*返回效果*：
```text
📊【群潜水成员扫描报告】
━━━━━━━━━━━━━━━━━━
👥 群总人数: 120 人
🎮 已绑定 MC 账号: 85 人 (已豁免)
🛡️ 管理员/白名单: 5 人 (已豁免)
🌱 新人保护期 (<3天): 4 人 (已豁免)
💬 未绑定近期发言: 16 人
━━━━━━━━━━━━━━━━━━
⚠️ 待清理人数 (未绑定且 ≥30天未发言): 10 人

📋 待清理成员名单 (最多展示前 30 名):
1. 张三 (12345678) - 最后发言: 45.2天前
2. 李四 (87654321) - 入群60.0天从未发言
...
💡 提示: 若要执行清理，请发送: /mc_clean 30
```

---

### 2. 执行清理 `/mc_clean`
```text
/mc_clean 30          # 发送后提示二次确认，防误触
/mc_clean 30 confirm  # 确认执行清理
/mc_clean 30 force    # 强制直接清理
```

---

### 3. 单人状态查询 `/mc_bind_check`
```text
/mc_bind_check              # 查询发送者自己的绑定状态与活跃天数
/mc_bind_check 12345678     # 查询指定 QQ 的绑定状态与活跃天数
/mc_bind_check @张三         # @群成员进行查询
```

---

### 4. 白名单管理 `/mc_whitelist`
```text
/mc_whitelist list               # 查看当前白名单列表
/mc_whitelist add 12345678       # 将 QQ 添加到白名单
/mc_whitelist del 12345678       # 将 QQ 从白名单中移除
```

---

### 5. 帮助信息 `/mc_help`
```text
/mc_help   # 查看指令列表和当前配置状态
```

---

## 🔒 安全与防封建议

1. **机器人身份**：执行踢人操作需要 QQ 机器人拥有群管理员权限。
2. **防风控延时**：不建议将 `kick_interval_seconds` 设置低于 1.0 秒。默认 1.5 秒能有效避免腾讯协议端风控。
3. **先扫描后清理**：每次大范围清理前，建议先使用 `/mc_scan` 确认待清理名单无误后再执行 `/mc_clean`。
