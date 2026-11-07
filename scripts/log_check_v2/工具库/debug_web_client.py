"""
DebugWeb 客户端封装

封装与游戏 DebugWeb 服务的所有交互。

真实 API 端点（从 DebugWeb 前端 JS 逆向获取）：
- POST /log/debug_input  {'cmd': '...'} — 发送命令到 debug console
- GET  /log/tail/<n>                    — 拉取从第 n 行开始的新日志
- GET  /mcp/diagnose                    — 健康检查 + 环境信息

所有命令类型（client_py / gm / server_py / client_py_json）统一走
debug_input，靠命令前缀区分：
  - 普通 Python 表达式 → 直接发
  - GM 指令 (item2/allitems) → 直接发（debug console 自动识别）
  - ~ 或 ! 开头 → server_py
  - :json 前缀 → client_py_json

用法:
    client = DebugWebClient(port=30001)
    client.send_command("item2 157018 1")
    client.send_command("print(G.player.id)")
    client.send_command("~print(G.player.bag_comp.hidden_items)")
"""
import json
import time
import requests


class DebugWebClient:

    def __init__(self, host="127.0.0.1", port=30001, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{host}:{port}"
        self._log_cursor = 0

    # ──────────── 基础请求 ────────────

    def _post_form(self, endpoint, data=None):
        url = f"{self.base_url}{endpoint}"
        try:
            resp = requests.post(url, data=data, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            print(f"[DebugWeb] POST 失败 {endpoint}: {e}")
            return None

    def _get(self, endpoint, params=None):
        url = f"{self.base_url}{endpoint}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            print(f"[DebugWeb] GET 失败 {endpoint}: {e}")
            return None

    def _get_json(self, endpoint, params=None):
        url = f"{self.base_url}{endpoint}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[DebugWeb] GET JSON 失败 {endpoint}: {e}")
            return None

    # ──────────── 命令执行（核心） ────────────

    def send_command(self, cmd):
        """
        发送命令到 DebugWeb console。
        debug console 会根据内容自动判断类型：
        - 普通 Python → client_py
        - GM 指令 (item2, allitems 等) → gm
        - ~ / ! 开头 → server_py
        - :json 前缀 → client_py_json
        """
        result = self._post_form("/log/debug_input", {"cmd": cmd})
        print(f"[cmd] 已发送: {cmd[:100]}")
        return result

    def client_py(self, command):
        """客户端执行 Python 表达式"""
        return self.send_command(command)

    def client_py_json(self, command):
        """客户端执行 Python，返回结构化结果（:json 前缀）"""
        if not command.startswith(":json"):
            command = f":json {command}"
        return self.send_command(command)

    def server_py(self, command):
        """服务端执行 Python 表达式"""
        if not command.startswith("~") and not command.startswith("!"):
            command = f"~{command}"
        return self.send_command(command)

    def gm(self, command):
        """执行 GM 指令（item2, allitems 等）"""
        return self.send_command(command)

    # ──────────── 日志读取 ────────────

    def get_log_tail(self, from_line=0):
        """
        拉取从 from_line 开始的新日志行。
        返回 (text, new_cursor)
        """
        data = self._get_json(f"/log/tail/{from_line}")
        if data:
            return data.get("text", ""), data.get("total", from_line)
        return "", from_line

    def get_new_log(self):
        """拉取自上次调用以来的新日志，自动维护 cursor"""
        text, self._log_cursor = self.get_log_tail(self._log_cursor)
        return text

    def get_all_log(self):
        """获取全部日志"""
        text, total = self.get_log_tail(0)
        self._log_cursor = total
        return text

    # ──────────── 便捷查询（发送命令 + 读取结果） ────────────

    def send_and_read(self, cmd, wait=1.0):
        """
        发送命令并等待日志输出，返回新增的日志文本。
        用于需要读取命令返回结果的场景。
        """
        before_cursor = self._log_cursor
        _ = self.get_new_log()

        self.send_command(cmd)
        time.sleep(wait)

        text = self.get_new_log()
        return text

    def get_storage_search_ids(self, tag1, tag2):
        """读取柜子 UI 筛选结果（需要先在游戏里打开柜子并选中分类）"""
        cmd = (
            f":json __result__ = sorted(set("
            f"row[3] for row in (G.ui.PanelStorageSearch.StorageSearch.search_cache or []) "
            f"if row[5] == ({tag1},{tag2}) and not row[6]))"
        )
        return self.send_command(cmd)

    def get_storage_ids_by_tag(self, tag1, tag2):
        """不依赖 UI，读所有柜子里 tag 匹配的物品ID"""
        cmd = (
            f":json __result__ = sorted(set("
            f"r.item.item_id "
            f"for b in G.player.bag_comp.iter_accessible_storage_bobjs(True, True, None) "
            f"for r in (b.target_react_comp.get_loot(G.player.id) or []) "
            f"if __import__('com.cdata.common_item_data', fromlist=['*'])"
            f".data.get(r.item.item_id, {{}}).get('all_item_tag') == ({tag1},{tag2})))"
        )
        return self.send_command(cmd)

    def get_expected_ids_from_config(self, tag1, tag2):
        """从配表读取指定 tag 应有的所有物品ID"""
        cmd = (
            f":json __result__ = sorted(["
            f"i for i,d in __import__('com.cdata.common_item_data', fromlist=['*'])"
            f".data.items() if d.get('all_item_tag') == ({tag1},{tag2})])"
        )
        return self.send_command(cmd)

    def get_missing_ids(self, tag1, tag2):
        """获取缺失物品ID（期望 - 实际）"""
        cmd = (
            f"~print(sorted(set(i for i,d in "
            f"__import__('com.cdata.common_item_data', fromlist=['*'])"
            f".data.items() if d.get('all_item_tag') == ({tag1},{tag2})) - "
            f"set(r.item.item_id for b in G.player.bag_comp"
            f".iter_accessible_storage_bobjs(True, True, None) "
            f"for r in (b.target_react_comp.get_loot(G.player.id) or []) "
            f"if __import__('com.cdata.common_item_data', fromlist=['*'])"
            f".data.get(r.item.item_id, {{}}).get('all_item_tag') == ({tag1},{tag2}))))"
        )
        return self.send_command(cmd)

    # ──────────── 端口探测 ────────────

    @classmethod
    def auto_detect(cls, host="127.0.0.1", port_start=30001, port_end=30020, timeout=2):
        """自动扫描端口，找到运行中的 DebugWeb"""
        for port in range(port_start, port_end + 1):
            try:
                resp = requests.get(
                    f"http://{host}:{port}/mcp/diagnose",
                    timeout=timeout
                )
                if resp.status_code == 200:
                    print(f"[auto] 发现 DebugWeb 在端口 {port}")
                    return cls(host=host, port=port)
            except requests.RequestException:
                continue
        print(f"[auto] 在 {port_start}~{port_end} 未找到 DebugWeb")
        return None

    def ping(self):
        """检测连接是否正常"""
        result = self._get("/mcp/diagnose")
        if result:
            print(f"[ping] DebugWeb 连接正常 @ {self.base_url}")
            return True
        print(f"[ping] DebugWeb 无响应 @ {self.base_url}")
        return False

    def __repr__(self):
        return f"DebugWebClient({self.base_url})"
