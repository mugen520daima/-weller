"""
LogWatcher + DebugWeb 实时监控面板

功能：
- 日志文件状态、实时监听、物品ID捕获
- DebugWeb 指令执行（client_py / gm / server_py）
- 操作历史记录（每条指令的发送/响应/时间）

用法:
    python monitor.py                  # 默认配置
    python monitor.py --port 5050      # 指定面板端口
    python monitor.py --no-watch       # 只看文件状态，不启动监听
    python monitor.py --debug-port 30001  # 指定 DebugWeb 端口
"""
import os
import sys
import time
import json
import argparse
import threading

from flask import Flask, jsonify, request as flask_request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "工具库"))
from collect_local_log import load_config, LogWatcher
from debug_web_client import DebugWebClient

app = Flask(__name__)

watcher = None
config = None
start_time = None
debug_client = None

recent_lines = []
recent_lines_lock = threading.Lock()
MAX_RECENT = 100

cmd_history = []
cmd_history_lock = threading.Lock()
MAX_CMD_HISTORY = 50


def _patched_process_line(self, line):
    match = self.pattern.search(line)
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "raw": line.strip()[:300],
        "matched": False,
        "item_id": None,
    }
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
        entry["matched"] = True
        entry["item_id"] = item_id

    with recent_lines_lock:
        recent_lines.append(entry)
        if len(recent_lines) > MAX_RECENT:
            del recent_lines[:len(recent_lines) - MAX_RECENT]


def get_file_info(path):
    info = {"path": path, "exists": False, "size": 0, "size_human": "0B", "lines": 0}
    if not path or not os.path.exists(path):
        return info
    info["exists"] = True
    size = os.path.getsize(path)
    info["size"] = size
    if size > 1024 * 1024:
        info["size_human"] = f"{size / 1024 / 1024:.1f} MB"
    elif size > 1024:
        info["size_human"] = f"{size / 1024:.1f} KB"
    else:
        info["size_human"] = f"{size} B"
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            info["lines"] = sum(1 for _ in f)
    except Exception:
        pass
    return info


def add_cmd_record(mode, command, result, success):
    with cmd_history_lock:
        cmd_history.append({
            "time": time.strftime("%H:%M:%S"),
            "mode": mode,
            "command": command,
            "result": str(result)[:500] if result else "",
            "success": success,
        })
        if len(cmd_history) > MAX_CMD_HISTORY:
            del cmd_history[:len(cmd_history) - MAX_CMD_HISTORY]


# ──────── API Routes ────────

@app.route("/")
def index():
    return DASHBOARD_HTML


@app.route("/api/status")
def api_status():
    log_path = config.get("local_log", {}).get("path", "") if config else ""
    file_info = get_file_info(log_path)

    with recent_lines_lock:
        lines_snapshot = list(recent_lines[-50:])

    ids_set = set()
    running = False
    if watcher:
        running = watcher.is_running
        with watcher._lock:
            ids_set = set(watcher._found_ids)

    uptime = 0
    if start_time:
        uptime = int(time.time() - start_time)

    with cmd_history_lock:
        history_snapshot = list(cmd_history[-20:])

    debug_ok = False
    if debug_client:
        debug_ok = True  # lazy check, actual ping in /api/debug-ping

    return jsonify({
        "file": file_info,
        "watcher": {
            "running": running,
            "uptime_seconds": uptime,
            "ids_count": len(ids_set),
            "ids": sorted(ids_set),
        },
        "pattern": config.get("local_log", {}).get("item_id_pattern", "") if config else "",
        "recent_lines": lines_snapshot,
        "total_lines_read": len(recent_lines),
        "debug_port": debug_client.port if debug_client else None,
        "cmd_history": history_snapshot,
    })


@app.route("/api/test-read")
def api_test_read():
    log_path = config.get("local_log", {}).get("path", "") if config else ""
    result = {"success": False, "lines": [], "error": ""}
    if not log_path:
        result["error"] = "config 中未配置 local_log.path"
        return jsonify(result)
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            result["lines"] = [f.readline().strip() for _ in range(10)]
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
    return jsonify(result)


@app.route("/api/start", methods=["POST"])
def api_start():
    global watcher, start_time
    if watcher and watcher.is_running:
        return jsonify({"ok": False, "msg": "already running"})
    watcher = LogWatcher(config)
    watcher.watch_timeout = 999999
    watcher._process_line = lambda line: _patched_process_line(watcher, line)
    watcher.start()
    start_time = time.time()
    with recent_lines_lock:
        recent_lines.clear()
    return jsonify({"ok": True, "msg": "watcher started"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global watcher
    if not watcher or not watcher.is_running:
        return jsonify({"ok": False, "msg": "not running"})
    ids, records = watcher.stop()
    return jsonify({"ok": True, "msg": f"stopped, captured {len(ids)} IDs", "ids": sorted(ids)})


@app.route("/api/debug-ping", methods=["POST"])
def api_debug_ping():
    global debug_client
    data = flask_request.get_json(silent=True) or {}
    port = data.get("port")
    if port:
        debug_client = DebugWebClient(port=int(port))
    if not debug_client:
        return jsonify({"ok": False, "msg": "未配置 DebugWeb 端口"})
    ok = debug_client.ping()
    add_cmd_record("ping", f"GET /mcp/diagnose @ port {debug_client.port}", "OK" if ok else "FAIL", ok)
    return jsonify({"ok": ok, "port": debug_client.port})


@app.route("/api/debug-exec", methods=["POST"])
def api_debug_exec():
    if not debug_client:
        return jsonify({"ok": False, "result": "", "error": "DebugWeb 未连接"})

    data = flask_request.get_json(silent=True) or {}
    mode = data.get("mode", "gm")
    command = data.get("command", "").strip()
    if not command:
        return jsonify({"ok": False, "result": "", "error": "指令为空"})

    add_cmd_record(mode, command, "[发送中...]", True)

    try:
        if mode == "client_py":
            result = debug_client.client_py(command)
        elif mode == "client_py_json":
            result = debug_client.client_py_json(command)
            result = json.dumps(result, ensure_ascii=False, indent=2) if result is not None else "null"
        elif mode == "server_py":
            result = debug_client.server_py(command)
        elif mode == "gm":
            result = debug_client.gm(command)
        else:
            return jsonify({"ok": False, "result": "", "error": f"未知模式: {mode}"})

        success = result is not None
        add_cmd_record(mode, command, result, success)
        return jsonify({"ok": success, "result": str(result) if result else "", "error": "" if success else "无响应"})
    except Exception as e:
        add_cmd_record(mode, command, str(e), False)
        return jsonify({"ok": False, "result": "", "error": str(e)})


@app.route("/api/debug-auto-detect", methods=["POST"])
def api_debug_auto_detect():
    global debug_client
    client = DebugWebClient.auto_detect()
    if client:
        debug_client = client
        add_cmd_record("auto-detect", f"扫描 30001~30020", f"找到端口 {client.port}", True)
        return jsonify({"ok": True, "port": client.port})
    add_cmd_record("auto-detect", "扫描 30001~30020", "未找到", False)
    return jsonify({"ok": False, "port": None})


# ──────── HTML Dashboard ────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>LogWatcher + DebugWeb Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,monospace;background:#0d1117;color:#c9d1d9;padding:20px;max-width:1200px;margin:0 auto}
h1{color:#58a6ff;margin-bottom:16px;font-size:22px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}
.card h2{font-size:14px;color:#8b949e;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px}
.stat{font-size:28px;font-weight:bold;color:#58a6ff}
.stat.ok{color:#3fb950}
.stat.err{color:#f85149}
.stat.warn{color:#d29922}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;margin:2px}
.tag.green{background:#238636;color:#fff}
.tag.red{background:#da3633;color:#fff}
.tag.gray{background:#30363d;color:#8b949e}
.tag.blue{background:#1f6feb;color:#fff}
.tag.yellow{background:#9e6a03;color:#fff}
.btn{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;margin-right:6px;margin-bottom:6px}
.btn-start{background:#238636;color:#fff}
.btn-start:hover{background:#2ea043}
.btn-stop{background:#da3633;color:#fff}
.btn-stop:hover{background:#f85149}
.btn-test{background:#1f6feb;color:#fff}
.btn-test:hover{background:#388bfd}
.btn-send{background:#8957e5;color:#fff}
.btn-send:hover{background:#a371f7}
.btn-ping{background:#d29922;color:#fff}
.btn-ping:hover{background:#e3b341}
.btn:disabled{opacity:0.4;cursor:not-allowed}
.controls{margin-bottom:16px;display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.log-box{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;max-height:350px;overflow-y:auto;font-size:13px;line-height:1.6}
.log-line{border-bottom:1px solid #21262d;padding:2px 0}
.log-line.matched{background:#1a2d1a;border-left:3px solid #3fb950;padding-left:8px}
.log-line .time{color:#8b949e;margin-right:8px}
.log-line .id-badge{background:#238636;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;margin-left:6px}
.ids-box{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}
.test-result{margin-top:10px;padding:10px;border-radius:6px;font-size:13px;display:none}
.test-result.ok{display:block;background:#1a2d1a;border:1px solid #238636}
.test-result.fail{display:block;background:#2d1a1a;border:1px solid #da3633}
#uptime{font-size:13px;color:#8b949e}

/* DebugWeb section */
.cmd-input-row{display:flex;gap:8px;margin-bottom:10px;align-items:center}
.cmd-input{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:10px 14px;font-size:14px;font-family:monospace}
.cmd-input:focus{outline:none;border-color:#58a6ff}
.mode-select{background:#21262d;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:10px;font-size:13px}
.cmd-history-box{max-height:300px;overflow-y:auto}
.cmd-entry{border-bottom:1px solid #21262d;padding:8px 0}
.cmd-entry .cmd-time{color:#8b949e;font-size:11px;margin-right:8px}
.cmd-entry .cmd-mode{font-size:11px;margin-right:6px}
.cmd-entry .cmd-text{color:#d2a8ff;font-family:monospace}
.cmd-entry .cmd-result{color:#8b949e;font-size:12px;margin-top:4px;white-space:pre-wrap;word-break:break-all;max-height:80px;overflow-y:auto}
.cmd-entry.fail .cmd-result{color:#f85149}
.cmd-entry.success .cmd-result{color:#3fb950}
.debug-status{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.dot.on{background:#3fb950}
.dot.off{background:#f85149}
.port-input{background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:6px 10px;width:80px;font-size:13px;text-align:center}
</style>
</head>
<body>
<h1>📡 LogWatcher + DebugWeb 监控面板</h1>

<!-- Log Watcher Controls -->
<div class="controls">
  <button class="btn btn-start" onclick="doStart()">▶ 开始监听</button>
  <button class="btn btn-stop" onclick="doStop()">⏹ 停止监听</button>
  <button class="btn btn-test" onclick="doTestRead()">🔍 测试读取</button>
  <span id="uptime"></span>
</div>

<div class="grid">
  <div class="card" style="margin-bottom:0">
    <h2>📄 日志文件</h2>
    <div id="file-path" style="font-size:13px;color:#8b949e;word-break:break-all;margin-bottom:8px">-</div>
    <div style="display:flex;gap:20px">
      <div><div class="stat" id="file-exists">-</div><div style="font-size:12px;color:#8b949e">状态</div></div>
      <div><div class="stat" id="file-size">-</div><div style="font-size:12px;color:#8b949e">大小</div></div>
      <div><div class="stat" id="file-lines">-</div><div style="font-size:12px;color:#8b949e">总行数</div></div>
    </div>
  </div>
  <div class="card" style="margin-bottom:0">
    <h2>🎯 采集状态</h2>
    <div style="display:flex;gap:20px">
      <div><div class="stat" id="watcher-status">-</div><div style="font-size:12px;color:#8b949e">监听状态</div></div>
      <div><div class="stat" id="ids-count">0</div><div style="font-size:12px;color:#8b949e">捕获ID</div></div>
      <div><div class="stat" id="lines-read">0</div><div style="font-size:12px;color:#8b949e">新增行</div></div>
    </div>
    <div style="margin-top:8px;font-size:12px;color:#8b949e">正则: <code id="pattern-display">-</code></div>
  </div>
</div>

<div class="card">
  <h2>🏷️ 已捕获的物品ID</h2>
  <div class="ids-box" id="ids-box"><span class="tag gray">暂无</span></div>
</div>

<div id="test-result" class="test-result"></div>

<!-- DebugWeb Command Panel -->
<div class="card">
  <h2>🎮 DebugWeb 指令台</h2>
  <div class="debug-status">
    <span class="dot" id="debug-dot"></span>
    <span id="debug-status-text" style="font-size:13px">未连接</span>
    <input type="number" class="port-input" id="debug-port" value="30001" placeholder="端口">
    <button class="btn btn-ping" onclick="doPing()">连接</button>
    <button class="btn btn-test" onclick="doAutoDetect()">🔍 自动探测</button>
  </div>
  <div class="cmd-input-row">
    <select class="mode-select" id="cmd-mode">
      <option value="gm">GM 指令</option>
      <option value="client_py">client_py</option>
      <option value="client_py_json">client_py_json</option>
      <option value="server_py">server_py</option>
    </select>
    <input type="text" class="cmd-input" id="cmd-input" placeholder="输入指令... (Enter 发送)" onkeydown="if(event.key==='Enter')doExec()">
    <button class="btn btn-send" onclick="doExec()">⚡ 发送</button>
  </div>
  <div style="margin-bottom:8px;font-size:12px;color:#8b949e">
    提示: GM指令如 <code>item2 157018 1</code> | Python如 <code>print(G.player.id)</code> | 每次发送自动清空输入框
  </div>
</div>

<!-- Command History -->
<div class="card">
  <h2>📋 操作历史</h2>
  <div class="cmd-history-box" id="cmd-history-box">
    <div style="color:#8b949e;text-align:center;padding:12px">暂无操作记录</div>
  </div>
</div>

<!-- Log Stream -->
<div class="card">
  <h2>📜 实时日志流 <span style="font-size:11px;color:#8b949e">(最近50行)</span></h2>
  <div class="log-box" id="log-box">
    <div style="color:#8b949e;text-align:center;padding:20px">等待启动监听...</div>
  </div>
</div>

<script>
let autoScroll = true;

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function fetchStatus() {
  try {
    const resp = await fetch('/api/status');
    const d = await resp.json();

    // file info
    document.getElementById('file-path').textContent = d.file.path || '-';
    const existsEl = document.getElementById('file-exists');
    if (d.file.exists) { existsEl.textContent = '✅'; existsEl.className = 'stat ok'; }
    else { existsEl.textContent = '❌'; existsEl.className = 'stat err'; }
    document.getElementById('file-size').textContent = d.file.size_human;
    document.getElementById('file-lines').textContent = d.file.lines.toLocaleString();

    // watcher
    const statusEl = document.getElementById('watcher-status');
    if (d.watcher.running) { statusEl.textContent = '运行中'; statusEl.className = 'stat ok'; }
    else { statusEl.textContent = '已停止'; statusEl.className = 'stat warn'; }
    document.getElementById('ids-count').textContent = d.watcher.ids_count;
    document.getElementById('lines-read').textContent = d.total_lines_read;
    document.getElementById('pattern-display').textContent = d.pattern || '-';

    // uptime
    if (d.watcher.running && d.watcher.uptime_seconds > 0) {
      const m = Math.floor(d.watcher.uptime_seconds / 60);
      const s = d.watcher.uptime_seconds % 60;
      document.getElementById('uptime').textContent = '运行 ' + m + '分' + s + '秒';
    } else { document.getElementById('uptime').textContent = ''; }

    // ids
    const idsBox = document.getElementById('ids-box');
    if (d.watcher.ids.length > 0) {
      idsBox.innerHTML = d.watcher.ids.map(id => '<span class="tag green">' + id + '</span>').join('');
    } else { idsBox.innerHTML = '<span class="tag gray">暂无</span>'; }

    // debug status
    const dot = document.getElementById('debug-dot');
    const dtxt = document.getElementById('debug-status-text');
    if (d.debug_port) {
      dot.className = 'dot on'; dtxt.textContent = '已连接 @ ' + d.debug_port;
      document.getElementById('debug-port').value = d.debug_port;
    } else { dot.className = 'dot off'; dtxt.textContent = '未连接'; }

    // command history
    const hbox = document.getElementById('cmd-history-box');
    if (d.cmd_history.length > 0) {
      hbox.innerHTML = d.cmd_history.slice().reverse().map(c => {
        const cls = c.success ? 'cmd-entry success' : 'cmd-entry fail';
        const modeTag = ({
          'gm': '<span class="tag yellow">GM</span>',
          'client_py': '<span class="tag blue">client_py</span>',
          'client_py_json': '<span class="tag blue">json</span>',
          'server_py': '<span class="tag red">server_py</span>',
          'ping': '<span class="tag gray">ping</span>',
          'auto-detect': '<span class="tag gray">detect</span>',
        })[c.mode] || '<span class="tag gray">' + c.mode + '</span>';
        return '<div class="' + cls + '">' +
          '<span class="cmd-time">' + c.time + '</span>' +
          modeTag +
          ' <span class="cmd-text">' + escHtml(c.command) + '</span>' +
          (c.result ? '<div class="cmd-result">' + escHtml(c.result) + '</div>' : '') +
          '</div>';
      }).join('');
    }

    // log lines
    const logBox = document.getElementById('log-box');
    if (d.recent_lines.length > 0) {
      logBox.innerHTML = d.recent_lines.map(l => {
        const cls = l.matched ? 'log-line matched' : 'log-line';
        const badge = l.matched ? '<span class="id-badge">ID: ' + l.item_id + '</span>' : '';
        return '<div class="' + cls + '"><span class="time">' + l.time + '</span>' + escHtml(l.raw) + badge + '</div>';
      }).join('');
      if (autoScroll) logBox.scrollTop = logBox.scrollHeight;
    } else if (!d.watcher.running) {
      logBox.innerHTML = '<div style="color:#8b949e;text-align:center;padding:20px">等待启动监听...</div>';
    }
  } catch(e) { console.error('fetch error', e); }
}

async function doStart() {
  const resp = await fetch('/api/start', {method:'POST'});
  const d = await resp.json();
  if (!d.ok) alert(d.msg);
  fetchStatus();
}

async function doStop() {
  const resp = await fetch('/api/stop', {method:'POST'});
  const d = await resp.json();
  if (d.ok) alert('已停止。捕获 ' + d.ids.length + ' 个ID: ' + (d.ids.join(', ') || '无'));
  fetchStatus();
}

async function doTestRead() {
  const el = document.getElementById('test-result');
  el.className = 'test-result'; el.style.display = 'none';
  const resp = await fetch('/api/test-read');
  const d = await resp.json();
  if (d.success) {
    el.className = 'test-result ok';
    el.innerHTML = '<b>✅ 读取成功!</b> 前10行:<br>' + d.lines.map(l => '<code>' + escHtml(l) + '</code>').join('<br>');
  } else {
    el.className = 'test-result fail';
    el.innerHTML = '<b>❌ 读取失败:</b> ' + escHtml(d.error);
  }
  el.style.display = 'block';
}

async function doPing() {
  const port = document.getElementById('debug-port').value;
  const resp = await fetch('/api/debug-ping', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({port: parseInt(port)})});
  const d = await resp.json();
  if (d.ok) { alert('✅ DebugWeb 连接成功 @ 端口 ' + d.port); }
  else { alert('❌ DebugWeb 无响应 @ 端口 ' + port); }
  fetchStatus();
}

async function doAutoDetect() {
  const resp = await fetch('/api/debug-auto-detect', {method:'POST'});
  const d = await resp.json();
  if (d.ok) { alert('✅ 自动发现 DebugWeb @ 端口 ' + d.port); document.getElementById('debug-port').value = d.port; }
  else { alert('❌ 在 30001~30020 未找到 DebugWeb'); }
  fetchStatus();
}

async function doExec() {
  const input = document.getElementById('cmd-input');
  const mode = document.getElementById('cmd-mode').value;
  const command = input.value.trim();
  if (!command) return;

  // 清空输入框（发送前清除）
  input.value = '';

  const resp = await fetch('/api/debug-exec', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode: mode, command: command})
  });
  const d = await resp.json();

  // 发送后输入框保持空（已清空）
  fetchStatus();

  if (!d.ok) {
    alert('指令执行失败: ' + (d.error || '未知错误'));
  }
}

setInterval(fetchStatus, 1000);
fetchStatus();
// 页面加载后自动尝试连接
setTimeout(async () => {
  const resp = await fetch('/api/debug-ping', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({port: parseInt(document.getElementById('debug-port').value)})});
  fetchStatus();
}, 500);
</script>
</body>
</html>"""


def main():
    global config, watcher, start_time, debug_client

    parser = argparse.ArgumentParser(description="LogWatcher + DebugWeb 监控面板")
    parser.add_argument("--port", type=int, default=5050, help="面板端口 (默认5050)")
    parser.add_argument("--no-watch", action="store_true", help="启动时不自动开始监听")
    parser.add_argument("--config", type=str, default=None, help="config.yaml 路径")
    parser.add_argument("--debug-port", type=int, default=30001, help="DebugWeb 端口 (默认30001)")
    args = parser.parse_args()

    config = load_config(args.config)
    debug_client = DebugWebClient(port=args.debug_port)

    if not args.no_watch:
        watcher = LogWatcher(config)
        watcher.watch_timeout = 999999
        watcher._process_line = lambda line: _patched_process_line(watcher, line)
        watcher.start()
        start_time = time.time()
        log_path = config.get('local_log', {}).get('path', '?')
        print(f"[monitor] LogWatcher 已启动，监听: {log_path}")

    print(f"[monitor] DebugWeb 目标: 127.0.0.1:{args.debug_port}")
    print(f"[monitor] 面板地址: http://127.0.0.1:{args.port}")
    print(f"[monitor] 按 Ctrl+C 停止")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
