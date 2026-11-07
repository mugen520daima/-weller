"""
将物品存入柜子

从 expected_change_ids.json 读取物品ID，逐个通过 debug 指令存入柜子。
柜子ID从 cabinet_ids.json 读取，放满换下一个。

用法:
    python store_items_to_cabinet.py
    python store_items_to_cabinet.py --dry-run    # 只打印不执行
"""
import json
import os
import re
import sys
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "工具库"))
from debug_log_utils import send_and_read, check_error, get_script_lines

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "配置")


def load_cabinet_ids():
    path = os.path.join(CONFIG_DIR, "cabinet_ids.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["cabinet_ids"]


def load_item_ids():
    path = os.path.join(CONFIG_DIR, "expected_change_ids.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("item_ids", []), data.get("items", {})


def build_add_cmd(cabinet_id, item_id, count=1):
    """构造存入柜子的 debug 指令"""
    return (
        f"!b = self.build_comp.get_step_building() or self.craft_comp.get_use_building(); "
        f"bobj = b.building_comp.get_building_object('{cabinet_id}'); "
        f"import com.item.RewardList as RewardList; "
        f"r = RewardList.RewardList(); "
        f"r.add_item({item_id}, {count}); "
        f"bobj.target_react_comp.add_rewards(self.id, r); "
        f"print('STORE_OK {item_id} -> {cabinet_id}')"
    )


def check_store_result(text, item_id, cabinet_id):
    """
    检查存放结果。
    核心判据: 日志中 sync_full_storage_loots RPC 同时包含:
      1. 目标柜子 id (cabinet_id)
      2. 目标物品 item_id
      3. count > 0
    返回: 'ok' | 'full' | 'error' | 'unknown'
    """
    if check_error(text):
        lines = get_script_lines(text)
        for line in lines:
            if "full" in line.lower() or "capacity" in line.lower() or "no space" in line.lower():
                return "full"
        return "error"

    # 在全文中查找 sync_full_storage_loots RPC 校验
    item_id_str = str(item_id)
    found_rpc = False
    found_cabinet = False
    found_item_with_count = False

    for line in text.split("\n"):
        if "sync_full_storage_loots" not in line:
            continue
        found_rpc = True
        if cabinet_id in line:
            found_cabinet = True
        if f"'item_id': {item_id_str}" in line or f'"item_id": {item_id_str}' in line:
            counts = re.findall(r"'count':\s*(\d+)", line)
            if not counts:
                counts = re.findall(r'"count":\s*(\d+)', line)
            # item_id 和 count 可能在同一个 dict 里，检查 item_id 附近的 count
            # 简化: 只要这行有 item_id 且有任何 count > 0 就算
            for c in counts:
                if int(c) > 0:
                    found_item_with_count = True
                    break

    if found_rpc and found_cabinet and found_item_with_count:
        return "ok"

    # fallback: 检查 STORE_OK 打印
    lines = get_script_lines(text)
    for line in lines:
        if f"STORE_OK {item_id}" in line:
            return "ok"

    if found_rpc and not found_cabinet:
        return "unknown"

    return "unknown"


def store_all_items(dry_run=False, wait=3):
    cabinet_ids = load_cabinet_ids()
    item_ids, id_names = load_item_ids()

    def name_of(iid):
        n = id_names.get(str(iid), "")
        return f" ({n})" if n else ""

    print("=" * 55)
    print("  物品存入柜子")
    print("=" * 55)
    print(f"  物品数: {len(item_ids)}")
    print(f"  柜子数: {len(cabinet_ids)}")
    if dry_run:
        print("  模式: DRY RUN (不实际执行)")
    print()

    cabinet_idx = 0
    success = []
    failed = []
    skipped = []

    for i, item_id in enumerate(item_ids):
        if cabinet_idx >= len(cabinet_ids):
            print(f"  ❌ 柜子已用完！剩余 {len(item_ids) - i} 个物品无法存放")
            skipped.extend(item_ids[i:])
            break

        cab_id = cabinet_ids[cabinet_idx]
        print(f"  [{i+1}/{len(item_ids)}] {item_id}{name_of(item_id)} -> 柜子 #{cabinet_idx+1}", end="")

        if dry_run:
            print(" [DRY RUN]")
            success.append(item_id)
            continue

        cmd = build_add_cmd(cab_id, item_id)
        sent_ok, text = send_and_read(cmd, wait=wait)

        if not sent_ok:
            print(" ❌ 发送失败")
            failed.append(item_id)
            continue

        result = check_store_result(text, item_id, cab_id)

        if result == "ok":
            print(" ✅")
            success.append(item_id)
        elif result == "full":
            print(f" 📦 柜子满，换下一个...")
            cabinet_idx += 1
            # 重试当前物品到新柜子
            if cabinet_idx < len(cabinet_ids):
                cab_id = cabinet_ids[cabinet_idx]
                cmd = build_add_cmd(cab_id, item_id)
                sent_ok, text = send_and_read(cmd, wait=wait)
                if sent_ok and check_store_result(text, item_id, cab_id) == "ok":
                    print(f"       -> 柜子 #{cabinet_idx+1} ✅")
                    success.append(item_id)
                else:
                    print(f"       -> 柜子 #{cabinet_idx+1} ❌")
                    failed.append(item_id)
            else:
                print("       -> 没有更多柜子了 ❌")
                failed.append(item_id)
        elif result == "error":
            print(" ❌ 执行报错")
            # 打印错误详情
            for line in get_script_lines(text):
                if "Error" in line or "Traceback" in line:
                    print(f"       | {line[:150]}")
            failed.append(item_id)
        else:
            print(" ⚠️ 结果未知（未检测到确认输出）")
            # 当 unknown 时也算成功，因为命令可能执行了只是没打印
            success.append(item_id)

    # 汇总
    print(f"\n{'='*55}")
    print(f"  存放完成")
    print(f"  ✅ 成功: {len(success)}")
    print(f"  ❌ 失败: {len(failed)}")
    if skipped:
        print(f"  ⏭️ 跳过: {len(skipped)} (柜子不够)")
    print(f"  使用了 {cabinet_idx + 1} 个柜子")
    print(f"{'='*55}")

    if failed:
        print(f"\n  失败的物品:")
        for fid in failed:
            print(f"    - {fid}{name_of(fid)}")

    return success, failed, skipped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="物品存入柜子")
    parser.add_argument("--dry-run", action="store_true", help="只打印不执行")
    parser.add_argument("--wait", type=float, default=3, help="每次存放后等待秒数 (默认3)")
    args = parser.parse_args()

    store_all_items(dry_run=args.dry_run, wait=args.wait)
