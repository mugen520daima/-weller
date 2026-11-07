"""
发送配表查询指令到 DebugWeb，捕获返回的物品ID列表，保存为结构化 JSON。
支持与期望改动清单比对（子集检查：改动ID是否全部包含在配表中）。

用法:
    python capture_config.py 3 12
    python capture_config.py 3 12 --compare expected_change_ids.json
    python capture_config.py --skip-capture --compare expected_change_ids.json
"""
import json
import time
import os
import sys
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "工具库"))
from debug_log_utils import execute_and_parse

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "测试结果")


def capture_config_ids(tag1=3, tag2=12):
    """发送配表查询，捕获返回的物品ID"""
    cmd = (
        f"~sorted([i for i,d in "
        f"__import__('com.cdata.common_item_data', fromlist=['*'])"
        f".data.items() if d.get('all_item_tag') == ({tag1},{tag2})])"
    )
    ok, ids = execute_and_parse(cmd, label=f"配表查询 tag=({tag1},{tag2})")
    if not ok:
        print(f"  ❌ 配表查询失败，无法继续")
        return None
    print(f"  ✅ 提取到 {len(ids)} 个物品ID")
    if ids:
        print(f"  范围: {ids[0]} ~ {ids[-1]}")
    return ids


def save_captured(ids, tag1=3, tag2=12, filename="captured_config_ids.json"):
    out = {
        "tag": [tag1, tag2],
        "source": "config_table",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(ids),
        "item_ids": ids,
    }
    path = os.path.join(OUT_DIR, filename)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  已保存: {path}")
    return path


def compare_with_expected(captured_ids, expected_path):
    with open(expected_path, "r", encoding="utf-8") as f:
        expected = json.load(f)

    change_ids = set(expected.get("item_ids", []))
    id_names = expected.get("items", {})
    config_ids = set(captured_ids)

    present = sorted(change_ids & config_ids)
    missing = sorted(change_ids - config_ids)

    def name_of(iid):
        n = id_names.get(str(iid), "")
        return f" ({n})" if n else ""

    print(f"\n{'='*50}")
    print(f"  配表比对结果  tag={expected.get('tag')}")
    print(f"  版本: {expected.get('version', '?')}")
    print(f"{'='*50}")
    print(f"  改动ID数量:  {len(change_ids)}")
    print(f"  配表ID数量:  {len(config_ids)}")
    print(f"  已包含:      {len(present)}/{len(change_ids)}")

    if not missing:
        print(f"\n  ✅ 全部通过 — {len(present)} 个改动ID都已在配表中生效")
        for pid in present:
            print(f"     ✓ {pid}{name_of(pid)}")
    else:
        print(f"\n  ❌ 缺失 {len(missing)} 个ID — 以下改动未在配表中找到:")
        for mid in missing:
            print(f"     ✗ {mid}{name_of(mid)}")
        if present:
            print(f"\n  已生效 ({len(present)} 个):")
            for pid in present:
                print(f"     ✓ {pid}{name_of(pid)}")

    result = {
        "tag": expected.get("tag"),
        "version": expected.get("version", ""),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "change_count": len(change_ids),
        "config_count": len(config_ids),
        "present_count": len(present),
        "missing_count": len(missing),
        "pass": len(missing) == 0,
        "present": present,
        "missing": missing,
    }
    report_path = os.path.join(OUT_DIR, "compare_result.json")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  报告已保存: {report_path}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="配表ID捕获与比对")
    parser.add_argument("tag1", nargs="?", type=int, default=3)
    parser.add_argument("tag2", nargs="?", type=int, default=12)
    parser.add_argument("--compare", type=str, default=None)
    parser.add_argument("--skip-capture", action="store_true")
    args = parser.parse_args()

    ids = None
    if args.skip_capture:
        cached = os.path.join(OUT_DIR, "captured_config_ids.json")
        with open(cached, "r", encoding="utf-8") as f:
            ids = json.load(f).get("item_ids", [])
        print(f"使用缓存: {len(ids)} 个ID")
    else:
        ids = capture_config_ids(args.tag1, args.tag2)
        if ids is None:
            sys.exit(1)
        save_captured(ids, args.tag1, args.tag2)
        print(f"\n配表 tag=({args.tag1},{args.tag2}) 共 {len(ids)} 个物品ID:")
        for i in range(0, len(ids), 10):
            print("  " + ", ".join(str(x) for x in ids[i:i+10]))

    if args.compare and ids is not None:
        compare_with_expected(ids, args.compare)
