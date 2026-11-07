"""
本地log实时采集模块

功能：实时 tail 本地log文件，持续提取新增行中的物品ID。
类似 tail -f，文件有新内容就立即读取解析。

用法：
    # 作为模块调用
    watcher = LogWatcher(config)
    watcher.start()        # 开始实时监听
    ...                    # 做其他操作（如点击柜子）
    ids, records = watcher.stop()  # 停止并获取结果

    # 直接运行测试
    python collect_local_log.py
"""
import re
import os
import time
import threading

try:
    import yaml
except ImportError:
    yaml = None


def load_config(config_path=None):
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "..", "配置", "config.yaml")
    if yaml is None:
        raise ImportError("需要安装PyYAML: pip install pyyaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class LogWatcher:
    """实时监听log文件，提取物品ID"""

    def __init__(self, config=None, config_path=None):
        if config is None:
            config = load_config(config_path)

        local_cfg = config.get("local_log", {})
        self.log_path = local_cfg.get("path", "")
        self.encoding = local_cfg.get("encoding", "utf-8")
        self.pattern = re.compile(local_cfg.get("item_id_pattern", "item_id[=:]\\s*(\\d+)"))
        self.poll_interval = local_cfg.get("poll_interval", 0.5)
        self.watch_timeout = local_cfg.get("watch_timeout", 120)

        self._found_ids = set()
        self._records = []
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def _tail(self):
        """从文件末尾开始实时读取新增内容"""
        if not os.path.exists(self.log_path):
            print(f"[本地log] 等待文件出现: {self.log_path}")
            # 等文件出现，最多等 watch_timeout
            waited = 0
            while not os.path.exists(self.log_path) and self._running:
                time.sleep(1)
                waited += 1
                if waited >= self.watch_timeout:
                    print(f"[本地log] 超时，文件未出现")
                    return

        with open(self.log_path, "r", encoding=self.encoding, errors="ignore") as f:
            # 跳到文件末尾，只读新增内容
            f.seek(0, 2)
            start_time = time.time()

            while self._running:
                line = f.readline()
                if line:
                    self._process_line(line)
                else:
                    # 没有新内容，短暂等待
                    time.sleep(self.poll_interval)

                # 超时检查
                if time.time() - start_time > self.watch_timeout:
                    print(f"[本地log] 监听超时({self.watch_timeout}s)，自动停止")
                    break

    def _process_line(self, line):
        """解析一行log，提取物品ID"""
        match = self.pattern.search(line)
        if match:
            item_id = match.group(1)
            with self._lock:
                self._found_ids.add(item_id)
                self._records.append({
                    "item_id": item_id,
                    "source": "local",
                    "timestamp": time.time(),
                    "raw_line": line.strip()
                })
            print(f"[本地log] 捕获ID: {item_id}")

    def start(self):
        """启动实时监听（后台线程）"""
        if self._running:
            print("[本地log] 已在运行中")
            return

        self._running = True
        self._thread = threading.Thread(target=self._tail, daemon=True)
        self._thread.start()
        print(f"[本地log] 开始实时监听: {self.log_path}")

    def stop(self):
        """停止监听，返回采集到的ID和记录"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

        with self._lock:
            ids = set(self._found_ids)
            records = list(self._records)

        print(f"[本地log] 停止监听，共采集 {len(ids)} 个物品ID")
        return ids, records

    def get_current(self):
        """获取当前已采集的数据（不停止监听）"""
        with self._lock:
            return set(self._found_ids), list(self._records)

    @property
    def is_running(self):
        return self._running


def collect_from_local_log_realtime(config_path=None, duration=None):
    """
    便捷函数：启动实时监听，等待指定时间后返回结果
    
    Args:
        config_path: yaml配置文件路径
        duration: 监听时长（秒），None则用config中的watch_timeout
    """
    config = load_config(config_path)
    watcher = LogWatcher(config)

    if duration is None:
        duration = config.get("local_log", {}).get("watch_timeout", 120)

    watcher.start()
    print(f"[本地log] 监听中... ({duration}秒后自动停止，按Ctrl+C提前结束)")

    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        print("\n[本地log] 手动中断")

    return watcher.stop()


if __name__ == "__main__":
    print("=" * 40)
    print("  本地Log实时采集测试")
    print("=" * 40)
    print("开始监听log文件，新增行中的物品ID会被实时捕获")
    print("按 Ctrl+C 停止\n")

    ids, records = collect_from_local_log_realtime(duration=30)
    print(f"\n采集结果: {len(ids)} 个ID")
    for r in records:
        print(f"  {r['item_id']} | {r['raw_line'][:80]}")
