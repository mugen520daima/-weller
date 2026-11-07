"""
物品配置变更 — 全流程自动化测试

流程:
  步骤1: 将改动物品存入柜子 + 逐条校验存入成功
  步骤2: 配表校验 (改动ID是否在配表中)
  步骤3: 柜子校验 (按分组ID查柜子物品是否符合预期)

每步: 校验指令成功 + 1分钟超时 + 失败/超时即停 + 原始日志写入报告

用法:
    python run_full_test.py
    python run_full_test.py --tag 3 12
    python run_full_test.py --skip-store   # 跳过步骤1(已存过)
"""
import json
import re
import time
import os
import sys
import argparse
import html as html_mod

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "工具库"))
from debug_log_utils import (
    BD, get_cursor, send_cmd, read_new_log,
    check_error, get_script_lines, parse_list_output
)

_BASE = os.path.dirname(__file__)
REPORT_DIR = os.path.join(_BASE, "..", "报告")
CONFIG_DIR = os.path.join(_BASE, "..", "配置")
EXPECTED_DEFAULT = os.path.join(CONFIG_DIR, "expected_change_ids.json")

STEP_TIMEOUT = 60  # 每步超时秒数


# ─── 带超时的指令执行 ───────────────────────────────────────

def send_and_wait(cmd, timeout=STEP_TIMEOUT, poll=1.0):
    """
    发送指令，轮询日志直到有 [SCRIPT] 输出或超时。
    返回 (ok, raw_log, timed_out)
    """
    cursor = get_cursor()
    if not send_cmd(cmd):
        return False, "", False

    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        time.sleep(poll)
        text, _ = read_new_log(cursor)
        if text.strip():
            script = get_script_lines(text)
            if script:
                # 再等一小会让日志刷完
                time.sleep(1.5)
                text, _ = read_new_log(cursor)
                return True, text, False

    # 超时
    text, _ = read_new_log(cursor)
    return True, text, True


def execute_step_cmd(cmd, label, timeout=STEP_TIMEOUT):
    """
    发送指令 + 超时检测 + 错误检测 + ID解析。
    返回 dict: {success, timed_out, error_msg, ids, raw_log}
    """
    print(f"    [{label}] 发送指令...")
    result = {
        "success": False,
        "timed_out": False,
        "error_msg": "",
        "ids": [],
        "raw_log": "",
    }
    try:
        ok, text, timed_out = send_and_wait(cmd, timeout=timeout)
        result["raw_log"] = text
        result["timed_out"] = timed_out

        if not ok:
            result["error_msg"] = "HTTP 发送失败"
            print(f"    [{label}] HTTP 发送失败")
            return result

        if timed_out:
            result["error_msg"] = f"超时 ({timeout}s)，未收到有效输出"
            print(f"    [{label}] 超时 ({timeout}s)")
            return result

        success, ids = parse_list_output(text, label=label)
        result["success"] = success
        result["ids"] = ids
        if not success:
            result["error_msg"] = "指令执行失败或无有效输出"
        return result

    except Exception as e:
        result["error_msg"] = str(e)
        print(f"    [{label}] 异常: {e}")
        return result


# ─── 步骤1: 存入物品 ──────────────────────────────────────

def step1_store_items(item_ids, id_names, timeout_per_item=STEP_TIMEOUT):
    """逐个物品存入柜子，逐条校验。"""
    cab_path = os.path.join(CONFIG_DIR, "cabinet_ids.json")
    with open(cab_path, "r", encoding="utf-8") as f:
        cabinet_ids = json.load(f)["cabinet_ids"]

    def name_of(iid):
        n = id_names.get(str(iid), "")
        return f" ({n})" if n else ""

    results = []  # list of {item_id, cabinet_id, status, raw_log, error_msg}
    cabinet_idx = 0
    overall_ok = True
    overall_timed_out = False

    print(f"    物品数: {len(item_ids)}, 柜子数: {len(cabinet_ids)}")

    for i, item_id in enumerate(item_ids):
        if cabinet_idx >= len(cabinet_ids):
            for remaining in item_ids[i:]:
                results.append({
                    "item_id": remaining, "cabinet_id": "-",
                    "status": "skipped", "raw_log": "", "error_msg": "柜子已用完"
                })
            overall_ok = False
            break

        cab_id = cabinet_ids[cabinet_idx]
        print(f"    [{i+1}/{len(item_ids)}] {item_id}{name_of(item_id)} -> 柜子 #{cabinet_idx+1}", end="", flush=True)

        cmd = (
            f"!b = self.build_comp.get_step_building() or self.craft_comp.get_use_building(); "
            f"bobj = b.building_comp.get_building_object('{cab_id}'); "
            f"import com.item.RewardList as RewardList; "
            f"r = RewardList.RewardList(); "
            f"r.add_item({item_id}, 1); "
            f"bobj.target_react_comp.add_rewards(self.id, r); "
            f"print('STORE_OK {item_id} -> {cab_id}')"
        )

        ok, text, timed_out = send_and_wait(cmd, timeout=timeout_per_item, poll=1.0)
        entry = {
            "item_id": item_id, "cabinet_id": cab_id,
            "status": "unknown", "raw_log": text, "error_msg": ""
        }

        if not ok:
            entry["status"] = "send_fail"
            entry["error_msg"] = "HTTP 发送失败"
            print(" HTTP发送失败")
            overall_ok = False
            results.append(entry)
            break  # 停止后续

        if timed_out:
            entry["status"] = "timeout"
            entry["error_msg"] = f"超时 ({timeout_per_item}s)"
            print(f" 超时")
            overall_ok = False
            overall_timed_out = True
            results.append(entry)
            break

        if check_error(text):
            # 检查是否柜子满
            full = False
            for line in text.split("\n"):
                if "full" in line.lower() or "capacity" in line.lower() or "no space" in line.lower():
                    full = True
            if full:
                entry["status"] = "full"
                entry["error_msg"] = "柜子已满"
                print(f" 柜子满", end="", flush=True)
                cabinet_idx += 1
                # retry
                if cabinet_idx < len(cabinet_ids):
                    cab_id = cabinet_ids[cabinet_idx]
                    cmd2 = cmd.replace(entry["cabinet_id"], cab_id)
                    ok2, text2, to2 = send_and_wait(cmd2, timeout=timeout_per_item)
                    entry["raw_log"] += "\n--- RETRY ---\n" + text2
                    if ok2 and not to2 and verify_store_in_log(text2, item_id, cab_id):
                        entry["status"] = "ok"
                        entry["cabinet_id"] = cab_id
                        print(f" -> #{cabinet_idx+1} OK")
                    else:
                        entry["status"] = "fail"
                        overall_ok = False
                        print(f" -> #{cabinet_idx+1} FAIL")
                        results.append(entry)
                        break
                else:
                    overall_ok = False
                    results.append(entry)
                    break
            else:
                entry["status"] = "error"
                entry["error_msg"] = "执行报错"
                print(" 报错")
                overall_ok = False
                results.append(entry)
                break
        else:
            # 校验: sync_full_storage_loots 中有 cabinet_id + item_id + count>0
            if verify_store_in_log(text, item_id, cab_id):
                entry["status"] = "ok"
                print(" OK")
            else:
                # fallback: STORE_OK
                script = get_script_lines(text)
                if any(f"STORE_OK {item_id}" in l for l in script):
                    entry["status"] = "ok"
                    print(" OK(print)")
                else:
                    entry["status"] = "unverified"
                    entry["error_msg"] = "未检测到 sync_full_storage_loots 确认"
                    print(" 未确认")

        results.append(entry)

    return {
        "step": "1-存入物品",
        "success": overall_ok,
        "timed_out": overall_timed_out,
        "total": len(item_ids),
        "ok_count": sum(1 for r in results if r["status"] == "ok"),
        "fail_count": sum(1 for r in results if r["status"] in ("fail", "error", "send_fail")),
        "timeout_count": sum(1 for r in results if r["status"] == "timeout"),
        "unverified_count": sum(1 for r in results if r["status"] == "unverified"),
        "details": results,
        "raw_log": "\n".join(r["raw_log"] for r in results if r["raw_log"]),
    }


def verify_store_in_log(text, item_id, cabinet_id):
    """sync_full_storage_loots 中同时有 cabinet_id + item_id + count>0"""
    item_id_str = str(item_id)
    for line in text.split("\n"):
        if "sync_full_storage_loots" not in line:
            continue
        if cabinet_id not in line:
            continue
        if f"'item_id': {item_id_str}" in line or f'"item_id": {item_id_str}' in line:
            counts = re.findall(r"'count':\s*(\d+)", line)
            if not counts:
                counts = re.findall(r'"count":\s*(\d+)', line)
            for c in counts:
                if int(c) > 0:
                    return True
    return False


# ─── 步骤2: 配表校验 ──────────────────────────────────────

def step2_config_check(tag1, tag2):
    cmd = (
        f"~sorted([i for i,d in "
        f"__import__('com.cdata.common_item_data', fromlist=['*'])"
        f".data.items() if d.get('all_item_tag') == ({tag1},{tag2})])"
    )
    r = execute_step_cmd(cmd, label=f"步骤2-配表查询({tag1},{tag2})")
    r["step"] = "2-配表校验"
    r["count"] = len(r["ids"])
    return r


# ─── 步骤3: 柜子校验 ──────────────────────────────────────

def step3_cabinet_check(tag1, tag2):
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
    r = execute_step_cmd(cmd, label=f"步骤3-柜子差异({tag1},{tag2})")
    r["step"] = "3-柜子校验"
    r["missing_ids"] = r.pop("ids")
    r["missing_count"] = len(r["missing_ids"])
    return r


# ─── 比对 ───────────────────────────────────────────────

def compare_results(expected, step2, step3):
    change_ids = set(expected.get("item_ids", []))
    id_names = expected.get("items", {})
    config_ids = set(step2.get("ids", []))
    missing_in_cabinet = set(step3.get("missing_ids", []))

    config_present = sorted(change_ids & config_ids)
    config_missing = sorted(change_ids - config_ids)
    in_cabinet = sorted(change_ids - missing_in_cabinet)
    not_in_cabinet = sorted(change_ids & missing_in_cabinet)

    return {
        "change_ids": sorted(change_ids),
        "id_names": id_names,
        "config_present": config_present,
        "config_missing": config_missing,
        "config_pass": len(config_missing) == 0,
        "in_cabinet": in_cabinet,
        "not_in_cabinet": not_in_cabinet,
        "cabinet_pass": len(not_in_cabinet) == 0,
    }


# ─── HTML 报告 ──────────────────────────────────────────

def generate_report(expected, step1, step2, step3, comparison, tag1, tag2, abort_reason=""):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ts_file = time.strftime("%Y%m%d_%H%M%S")
    id_names = expected.get("items", {})
    esc = html_mod.escape

    def name_of(iid):
        n = id_names.get(str(iid), "")
        return esc(n) if n else str(iid)

    s1_ok = step1 and step1.get("success", False)
    s2_ok = step2 and step2.get("success", False)
    s3_ok = step3 and step3.get("success", False)

    has_comparison = comparison is not None
    config_pass = comparison["config_pass"] if has_comparison else False
    cabinet_pass = comparison["cabinet_pass"] if has_comparison else False
    all_pass = s1_ok and config_pass and cabinet_pass

    def icon(ok): return "&#x2705;" if ok else "&#x274C;"
    def badge(ok, t="PASS", f="FAIL"):
        cls = "pass" if ok else "fail"
        txt = t if ok else f
        return f'<span class="badge {cls}">{txt}</span>'

    overall_badge = badge(all_pass, "全部通过", "存在失败项")

    # Step1 detail
    s1_detail = ""
    if step1:
        if step1.get("timed_out"):
            s1_detail = f"超时，成功 {step1['ok_count']}/{step1['total']}"
        elif step1["success"]:
            s1_detail = f"成功存入 {step1['ok_count']}/{step1['total']}"
        else:
            s1_detail = f"失败，成功 {step1['ok_count']}/{step1['total']}, 失败 {step1['fail_count']}"
    else:
        s1_detail = "跳过"

    s2_detail = f"获取到 {step2['count']} 个配表ID" if s2_ok else (step2.get("error_msg", "未执行") if step2 else "未执行")
    s3_detail = f"柜子缺失 {step3['missing_count']} 个" if s3_ok else (step3.get("error_msg", "未执行") if step3 else "未执行")

    # ─── Store items detail rows ───
    store_rows = ""
    if step1 and step1.get("details"):
        for d in step1["details"]:
            iid = d["item_id"]
            st = d["status"]
            cls = "pass-cell" if st == "ok" else "fail-cell"
            st_text = {"ok": "成功", "fail": "失败", "error": "报错", "timeout": "超时",
                       "send_fail": "发送失败", "full": "柜子满", "skipped": "跳过",
                       "unverified": "未确认"}.get(st, st)
            icn = "&#x2705;" if st == "ok" else ("&#x26A0;" if st == "unverified" else "&#x274C;")
            store_rows += f'<tr><td>{iid}</td><td>{name_of(iid)}</td><td>{esc(d["cabinet_id"][:20])}</td><td class="{cls}">{icn} {st_text}</td></tr>\n'

    # ─── Config check rows ───
    config_rows = ""
    if has_comparison:
        for iid in comparison["change_ids"]:
            if iid in comparison["config_present"]:
                config_rows += f'<tr><td>{iid}</td><td>{name_of(iid)}</td><td class="pass-cell">&#x2705; 已生效</td></tr>\n'
            else:
                config_rows += f'<tr><td>{iid}</td><td>{name_of(iid)}</td><td class="fail-cell">&#x274C; 未找到</td></tr>\n'

    # ─── Cabinet check rows ───
    cabinet_rows = ""
    if has_comparison:
        for iid in comparison["change_ids"]:
            if iid in comparison["in_cabinet"]:
                cabinet_rows += f'<tr><td>{iid}</td><td>{name_of(iid)}</td><td class="pass-cell">&#x2705; 在柜子中</td></tr>\n'
            else:
                cabinet_rows += f'<tr><td>{iid}</td><td>{name_of(iid)}</td><td class="fail-cell">&#x274C; 不在柜子</td></tr>\n'

    # ─── Change list rows ───
    change_rows = ""
    for i, iid in enumerate(expected.get("item_ids", []), 1):
        change_rows += f'<tr><td>{i}</td><td>{iid}</td><td>{name_of(iid)}</td></tr>\n'

    # ─── Raw log section ───
    raw_logs = []
    if step1 and step1.get("raw_log"):
        raw_logs.append(("步骤1-存入物品", step1["raw_log"]))
    if step2 and step2.get("raw_log"):
        raw_logs.append(("步骤2-配表校验", step2["raw_log"]))
    if step3 and step3.get("raw_log"):
        raw_logs.append(("步骤3-柜子校验", step3["raw_log"]))

    log_html = ""
    for title, log_text in raw_logs:
        escaped_log = esc(log_text[-5000:] if len(log_text) > 5000 else log_text)
        log_html += f'''
<div class="log-section">
  <h3>{esc(title)}</h3>
  <pre class="log-pre">{escaped_log}</pre>
</div>'''

    # ─── Abort notice ───
    abort_html = ""
    if abort_reason:
        abort_html = f'<div class="warning">测试中断: {esc(abort_reason)}</div>'

    # ─── Store section ───
    store_section = ""
    if step1 and step1.get("details"):
        s1_badge = badge(s1_ok, "PASS", "FAIL")
        store_section = f'''
<div class="card">
  <h2>2. 存入物品 {s1_badge}</h2>
  <div class="stats">
    <div><span class="s-label">总数:</span><span class="s-value">{step1["total"]}</span></div>
    <div><span class="s-label">成功:</span><span class="s-value">{step1["ok_count"]}</span></div>
    <div><span class="s-label">失败:</span><span class="s-value">{step1["fail_count"]}</span></div>
    <div><span class="s-label">未确认:</span><span class="s-value">{step1.get("unverified_count", 0)}</span></div>
  </div>
  <table>
    <thead><tr><th>物品ID</th><th>物品名称</th><th>柜子</th><th>状态</th></tr></thead>
    <tbody>
{store_rows}    </tbody>
  </table>
</div>'''

    # ─── Config section ───
    config_section = ""
    if has_comparison:
        config_section = f'''
<div class="card">
  <h2>3. 配表校验 {badge(config_pass)}</h2>
  <div class="stats">
    <div><span class="s-label">改动ID:</span><span class="s-value">{len(comparison["change_ids"])}</span></div>
    <div><span class="s-label">配表ID:</span><span class="s-value">{step2["count"] if step2 else 0}</span></div>
    <div><span class="s-label">已包含:</span><span class="s-value">{len(comparison["config_present"])}/{len(comparison["change_ids"])}</span></div>
  </div>
  <table>
    <thead><tr><th>物品ID</th><th>物品名称</th><th>配表状态</th></tr></thead>
    <tbody>
{config_rows}    </tbody>
  </table>
</div>'''

    # ─── Cabinet section ───
    cabinet_section = ""
    if has_comparison:
        cabinet_section = f'''
<div class="card">
  <h2>4. 柜子校验 {badge(cabinet_pass)}</h2>
  <div class="stats">
    <div><span class="s-label">在柜子:</span><span class="s-value">{len(comparison["in_cabinet"])}</span></div>
    <div><span class="s-label">不在柜子:</span><span class="s-value">{len(comparison["not_in_cabinet"])}</span></div>
  </div>
  <table>
    <thead><tr><th>物品ID</th><th>物品名称</th><th>柜子状态</th></tr></thead>
    <tbody>
{cabinet_rows}    </tbody>
  </table>
</div>'''

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>物品配置变更测试报告 {esc(ts)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#0d1117;color:#c9d1d9;padding:24px;max-width:1100px;margin:0 auto}}
h1{{color:#58a6ff;font-size:22px;margin-bottom:6px}}
.subtitle{{color:#8b949e;font-size:13px;margin-bottom:20px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:18px;margin-bottom:16px}}
.card h2{{font-size:15px;color:#8b949e;margin-bottom:12px;text-transform:uppercase;letter-spacing:.5px}}
.card h3{{font-size:14px;color:#58a6ff;margin:12px 0 6px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{background:#21262d;color:#8b949e;text-align:left;padding:8px 12px;font-weight:600;border-bottom:1px solid #30363d}}
td{{padding:8px 12px;border-bottom:1px solid #21262d}}
tr:hover{{background:#1c2129}}
.pass-cell{{color:#3fb950;font-weight:600}}
.fail-cell{{color:#f85149;font-weight:600}}
.badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:700;letter-spacing:.3px}}
.badge.pass{{background:#238636;color:#fff}}
.badge.fail{{background:#da3633;color:#fff}}
.header-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.header-grid .item{{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #21262d}}
.header-grid .item .label{{color:#8b949e}}
.header-grid .item .value{{color:#c9d1d9;font-weight:600}}
.summary-row td{{font-weight:700;font-size:15px}}
.warning{{background:#2d1f00;border:1px solid #9e6a03;border-radius:6px;padding:12px 16px;margin-top:12px;color:#d29922;font-size:13px}}
.stats{{display:flex;gap:24px;margin-bottom:12px;font-size:13px}}
.stats .s-label{{color:#8b949e}}
.stats .s-value{{color:#c9d1d9;font-weight:700;margin-left:4px}}
.log-section{{margin-top:12px}}
.log-pre{{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:12px;font-size:12px;color:#8b949e;overflow-x:auto;max-height:400px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}}
</style>
</head>
<body>

<h1>物品配置变更测试报告</h1>
<p class="subtitle">生成于 {esc(ts)}</p>

<div class="card">
  <h2>测试信息</h2>
  <div class="header-grid">
    <div class="item"><span class="label">测试时间</span><span class="value">{esc(ts)}</span></div>
    <div class="item"><span class="label">分组 Tag</span><span class="value">({tag1}, {tag2})</span></div>
    <div class="item"><span class="label">版本</span><span class="value">{esc(str(expected.get('version', '-')))}</span></div>
    <div class="item"><span class="label">改动物品数</span><span class="value">{len(expected.get('item_ids', []))}</span></div>
    <div class="item"><span class="label">DebugWeb</span><span class="value">{esc(BD)}</span></div>
    <div class="item"><span class="label">总结</span><span class="value">{overall_badge}</span></div>
  </div>
  {abort_html}
</div>

<div class="card">
  <h2>1. 改动物品清单</h2>
  <table>
    <thead><tr><th>#</th><th>物品ID</th><th>物品名称</th></tr></thead>
    <tbody>
{change_rows}    </tbody>
  </table>
</div>

{store_section}
{config_section}
{cabinet_section}

<div class="card">
  <h2>5. 测试步骤总结</h2>
  <table>
    <thead><tr><th>步骤</th><th>说明</th><th>状态</th><th>详情</th></tr></thead>
    <tbody>
      <tr><td>步骤1</td><td>存入物品</td><td>{icon(s1_ok)}</td><td>{esc(s1_detail)}</td></tr>
      <tr><td>步骤2</td><td>配表校验</td><td>{icon(s2_ok)}</td><td>{esc(s2_detail)}</td></tr>
      <tr><td>步骤3</td><td>柜子校验</td><td>{icon(s3_ok)}</td><td>{esc(s3_detail)}</td></tr>
      <tr><td>配表</td><td>比对结果</td><td colspan="2">{badge(config_pass) if has_comparison else '未执行'}</td></tr>
      <tr><td>柜子</td><td>比对结果</td><td colspan="2">{badge(cabinet_pass) if has_comparison else '未执行'}</td></tr>
      <tr class="summary-row"><td>总结</td><td colspan="3">{overall_badge}</td></tr>
    </tbody>
  </table>
</div>

<div class="card">
  <h2>6. 原始日志</h2>
  {log_html if log_html else '<p style="color:#8b949e">无日志</p>'}
</div>

</body>
</html>"""

    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f"test_report_{ts_file}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    json_path = os.path.join(REPORT_DIR, f"test_report_{ts_file}.json")
    json_data = {
        "timestamp": ts,
        "tag": [tag1, tag2],
        "version": expected.get("version", ""),
        "abort_reason": abort_reason,
        "steps": {
            "step1": {k: v for k, v in (step1 or {}).items() if k != "raw_log"} if step1 else None,
            "step2": {k: v for k, v in (step2 or {}).items() if k != "raw_log"} if step2 else None,
            "step3": {k: v for k, v in (step3 or {}).items() if k != "raw_log"} if step3 else None,
        },
        "comparison": {
            "config_pass": config_pass,
            "config_present": comparison["config_present"] if has_comparison else [],
            "config_missing": comparison["config_missing"] if has_comparison else [],
            "cabinet_pass": cabinet_pass,
            "in_cabinet": comparison["in_cabinet"] if has_comparison else [],
            "not_in_cabinet": comparison["not_in_cabinet"] if has_comparison else [],
        } if has_comparison else None,
        "all_pass": all_pass,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    return report_path, json_path


# ─── Main ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="物品配置变更全流程测试")
    parser.add_argument("--tag", nargs=2, type=int, default=[3, 12], metavar=("T1", "T2"))
    parser.add_argument("--expected", type=str, default=EXPECTED_DEFAULT)
    parser.add_argument("--skip-store", action="store_true", help="跳过步骤1(物品已存入)")
    args = parser.parse_args()
    tag1, tag2 = args.tag

    print("=" * 55)
    print("  物品配置变更 — 全流程自动化测试")
    print("=" * 55)

    expected = load_expected(args.expected)
    change_ids = expected.get("item_ids", [])
    id_names = expected.get("items", {})
    print(f"\n[0] 加载改动清单: {args.expected}")
    print(f"    版本: {expected.get('version', '?')}")
    print(f"    tag: ({tag1}, {tag2})")
    print(f"    改动物品: {len(change_ids)} 个")

    step1 = None
    step2 = None
    step3 = None
    comparison = None
    abort_reason = ""

    # ─── 步骤1: 存入物品 ───
    if args.skip_store:
        print(f"\n{'---'*18}")
        print("[步骤1] 跳过 (--skip-store)")
        step1 = {"step": "1-存入物品", "success": True, "total": len(change_ids),
                  "ok_count": len(change_ids), "fail_count": 0, "timeout_count": 0,
                  "unverified_count": 0, "details": [], "raw_log": "(跳过)"}
    else:
        print(f"\n{'---'*18}")
        print("[步骤1] 存入物品到柜子")
        step1 = step1_store_items(change_ids, id_names)
        if not step1["success"]:
            if step1.get("timed_out"):
                abort_reason = "步骤1 存入物品超时"
            else:
                abort_reason = f"步骤1 存入物品失败 ({step1['ok_count']}/{step1['total']})"
            print(f"\n    停止: {abort_reason}")
            html_p, json_p = generate_report(expected, step1, step2, step3, comparison, tag1, tag2, abort_reason)
            print(f"\n    报告: {html_p}")
            return

    # ─── 步骤2: 配表校验 ───
    print(f"\n{'---'*18}")
    print("[步骤2] 配表校验")
    step2 = step2_config_check(tag1, tag2)
    if not step2["success"]:
        if step2.get("timed_out"):
            abort_reason = "步骤2 配表查询超时"
        else:
            abort_reason = f"步骤2 配表查询失败: {step2.get('error_msg', '未知')}"
        print(f"\n    停止: {abort_reason}")
        html_p, json_p = generate_report(expected, step1, step2, step3, comparison, tag1, tag2, abort_reason)
        print(f"\n    报告: {html_p}")
        return

    # ─── 步骤3: 柜子校验 ───
    print(f"\n{'---'*18}")
    print("[步骤3] 柜子物品校验")
    step3 = step3_cabinet_check(tag1, tag2)
    if not step3["success"]:
        if step3.get("timed_out"):
            abort_reason = "步骤3 柜子查询超时"
        else:
            abort_reason = f"步骤3 柜子查询失败: {step3.get('error_msg', '未知')}"
        print(f"\n    停止: {abort_reason}")
        html_p, json_p = generate_report(expected, step1, step2, step3, comparison, tag1, tag2, abort_reason)
        print(f"\n    报告: {html_p}")
        return

    # ─── 比对 ───
    print(f"\n{'---'*18}")
    print("[比对] 综合分析")
    comparison = compare_results(expected, step2, step3)

    print(f"\n    配表校验: {'PASS' if comparison['config_pass'] else 'FAIL'}"
          f" ({len(comparison['config_present'])}/{len(change_ids)})")
    print(f"    柜子校验: {'PASS' if comparison['cabinet_pass'] else 'FAIL'}"
          f" (在柜子 {len(comparison['in_cabinet'])}, 不在 {len(comparison['not_in_cabinet'])})")

    # ─── 报告 ───
    print(f"\n{'---'*18}")
    print("[报告] 生成测试报告...")
    html_p, json_p = generate_report(expected, step1, step2, step3, comparison, tag1, tag2)
    print(f"    HTML: {html_p}")
    print(f"    JSON: {json_p}")

    all_pass = step1["success"] and comparison["config_pass"] and comparison["cabinet_pass"]
    print(f"\n{'='*55}")
    print(f"  {'测试全部通过' if all_pass else '测试存在失败项'}")
    print(f"{'='*55}")


def load_expected(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    main()
