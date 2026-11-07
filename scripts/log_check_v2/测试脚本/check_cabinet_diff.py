"""
步骤4: 查看柜子里缺了哪些物品（配表期望 - 柜子实际）
同时也查背包里有哪些匹配tag的物品
"""
import json
import time
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "工具库"))
from debug_log_utils import execute_and_parse

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "测试结果")


def main():
    print("=" * 55)
    print("  步骤4: 柜子差异检查 (配表期望 - 柜子实际)")
    print("=" * 55)

    # 1. 柜子差异（期望 - 实际）
    print("\n[1] 柜子差异指令...")
    diff_cmd = (
        "~print(sorted(set(i for i,d in "
        "__import__('com.cdata.common_item_data', fromlist=['*'])"
        ".data.items() if d.get('all_item_tag') == (3,12)) - "
        "set(r.item.item_id for b in G.player.bag_comp"
        ".iter_accessible_storage_bobjs(True, True, None) "
        "for r in (b.target_react_comp.get_loot(G.player.id) or []) "
        "if __import__('com.cdata.common_item_data', fromlist=['*'])"
        ".data.get(r.item.item_id, {}).get('all_item_tag') == (3,12))))"
    )
    ok, missing_in_cabinet = execute_and_parse(diff_cmd, label="柜子差异")
    if not ok:
        print("    ❗ 柜子差异指令执行失败，无法继续")
        return

    if not missing_in_cabinet:
        print("    ✅ 返回 [] — 柜子里包含所有配表物品，无缺失")
    else:
        print(f"    📋 柜子缺失 {len(missing_in_cabinet)} 个物品:")
        for mid in missing_in_cabinet:
            print(f"       - {mid}")

    # 2. 综合判断
    print("\n[2] 综合判断...")
    expected_path = os.path.join(os.path.dirname(__file__), "..", "配置", "expected_change_ids.json")
    if not os.path.exists(expected_path):
        print("    未找到 expected_change_ids.json，跳过综合比对")
        return

    with open(expected_path, "r", encoding="utf-8") as f:
        expected = json.load(f)
    change_ids = set(expected.get("item_ids", []))
    id_names = expected.get("items", {})

    def name_of(iid):
        n = id_names.get(str(iid), "")
        return f" ({n})" if n else ""

    in_cabinet = change_ids - set(missing_in_cabinet)
    not_in_cabinet = change_ids & set(missing_in_cabinet)

    print(f"    改动ID:    {len(change_ids)} 个")
    print(f"    在柜子:    {len(in_cabinet)} 个")
    print(f"    不在柜子:  {len(not_in_cabinet)} 个")

    if not not_in_cabinet:
        print(f"\n    ✅ 所有改动物品都在柜子中")
    else:
        print(f"\n    ❌ {len(not_in_cabinet)} 个改动物品不在柜子中:")
        for nid in sorted(not_in_cabinet):
            print(f"       ✗ {nid}{name_of(nid)}")
        if in_cabinet:
            print(f"\n    已在柜子 ({len(in_cabinet)} 个):")
            for cid in sorted(in_cabinet):
                print(f"       ✓ {cid}{name_of(cid)}")

    result = {
        "tag": [3, 12],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_change": len(change_ids),
        "in_cabinet": sorted(in_cabinet),
        "in_cabinet_count": len(in_cabinet),
        "not_in_cabinet": sorted(not_in_cabinet),
        "not_in_cabinet_count": len(not_in_cabinet),
        "pass": len(not_in_cabinet) == 0,
    }
    rp = os.path.join(OUT_DIR, "step4_diff_result.json")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n    结果已保存: {rp}")


if __name__ == "__main__":
    main()
