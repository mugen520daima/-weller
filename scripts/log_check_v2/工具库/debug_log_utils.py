"""
DebugWeb 日志解析公共工具

所有通过 DebugWeb 发送指令并解析日志的脚本共用此模块。
核心原则：每次指令执行必须先判断成功再使用结果。
"""
import re
import time
import requests

BD = "http://127.0.0.1:30001"


def get_cursor():
    r = requests.get(f"{BD}/log/tail/0", timeout=10)
    return r.json().get("total", 0)


def send_cmd(cmd):
    r = requests.post(f"{BD}/log/debug_input", data={"cmd": cmd}, timeout=10)
    return r.status_code == 200


def read_new_log(cursor):
    r = requests.get(f"{BD}/log/tail/{cursor}", timeout=10)
    d = r.json()
    return d.get("text", ""), d.get("total", cursor)


def send_and_read(cmd, wait=3):
    """发送指令，等待，读取新日志"""
    cursor = get_cursor()
    ok = send_cmd(cmd)
    if not ok:
        return False, ""
    time.sleep(wait)
    text, _ = read_new_log(cursor)
    return True, text


def check_error(text):
    """检查日志中是否有 Python 报错"""
    for line in text.split("\n"):
        if "Traceback" in line or "SyntaxError" in line:
            return True
        if "[0;31m" in line and "Error" in line:
            return True
    return False


def get_script_lines(text):
    """提取 [SCRIPT] 行，去掉时间戳前缀"""
    results = []
    for line in text.split("\n"):
        if "[SCRIPT]" in line:
            content = re.sub(r".*\[SCRIPT\]\s*", "", line).strip()
            # 去掉 ANSI 颜色码
            content = re.sub(r"\x1b\[[0-9;]*m", "", content)
            if content:
                results.append(content)
    return results


def parse_list_output(text, label="指令"):
    """
    解析 print([...]) 的日志输出。

    返回 (success, ids):
      - (True, [])        → 指令成功，返回空列表
      - (True, [id,...])   → 指令成功，返回ID列表
      - (False, [])        → 指令失败或无输出
    """
    # 检查 HTTP 发送是否成功
    if not text:
        print(f"    ❗ [{label}] 无日志输出，指令可能未发送成功")
        return False, []

    # 检查 Python 执行报错
    if check_error(text):
        print(f"    ❗ [{label}] 指令执行报错:")
        for line in get_script_lines(text):
            if "Traceback" in line or "Error" in line or "File" in line:
                print(f"      | {line[:200]}")
        return False, []

    script_lines = get_script_lines(text)
    if not script_lines:
        print(f"    ❗ [{label}] 无 [SCRIPT] 输出")
        return False, []

    # 检查明确的 [] 空列表
    for content in script_lines:
        if content.strip() == "[]":
            return True, []

    # 提取7位数字ID
    ids = []
    for content in script_lines:
        if content in ("None", "~"):
            continue
        matches = re.findall(r"\b(\d{7})\b", content)
        ids.extend(int(m) for m in matches)

    ids = sorted(set(ids))
    if ids:
        return True, ids

    # 有输出但没提取到ID
    print(f"    ⚠️ [{label}] 有输出但未提取到7位ID，原始内容:")
    for line in script_lines[:5]:
        print(f"      | {line[:200]}")
    return False, []


def execute_and_parse(cmd, label="指令", wait=3):
    """
    发送指令 → 等待 → 读日志 → 解析结果。
    返回 (success, ids)。失败时打印错误信息。
    """
    print(f"    [{label}] 发送指令...")
    sent_ok, text = send_and_read(cmd, wait=wait)
    if not sent_ok:
        print(f"    ❗ [{label}] HTTP 发送失败")
        return False, []
    return parse_list_output(text, label=label)
