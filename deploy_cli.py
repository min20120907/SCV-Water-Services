#!/usr/bin/env python3
"""
SCV Water 一鍵自動化部署 CLI
使用 Qwen3.8-27B 即時推導感測器規格 -> 生成驅動 -> SSH 部署至樹莓派 -> 生成 Fritzing 配線圖 -> 註冊至 App
"""
import argparse
import json
import os
import sys
import time
import urllib.request

DEFAULT_SERVICE = os.environ.get("SCV_DEPLOY_SERVICE", "http://127.0.0.1:8767")
API_KEY = "scv-water-2026-secure-key"

def main():
    parser = argparse.ArgumentParser(description="SCV Water 一鍵自動化感測器部署器")
    parser.add_argument("model", help="感測器型號或中文規格描述 (例如: 'BH1750 光照強度感測器', 'YF-S201 水流量計')")
    parser.add_argument("--site", default="1", help="綁定之場域 ID (預設: 1)")
    parser.add_argument("--site-name", default="Emulator Bathroom", help="場域名稱")
    parser.add_argument("--host", default="ssh.labertus.online", help="樹莓派 SSH 主機名稱 (預設: ssh.labertus.online)")
    parser.add_argument("--user", default="pi", help="SSH 使用者名稱 (預設: pi，自動 fallback 至 admin)")
    parser.add_argument("--port", type=int, default=22, help="SSH 連接埠")
    parser.add_argument("--device-id", default=None, help="自訂裝置 ID (預設由 AI 自動命名)")
    parser.add_argument("--out-diagram", default="/home/min20120907/Documents/Academic_and_Research/scv_water/latest_wiring_diagram.png", help="配線圖輸出路徑")

    args = parser.parse_args()

    print(f"🚀 [1/4] 開始一鍵自動化部署流水線...")
    print(f"   • 感測器型號: {args.model}")
    print(f"   • 目標場域: {args.site} ({args.site_name})")
    print(f"   • 部署主機: {args.host}:{args.port}")

    payload = {
        "model": args.model,
        "site_id": args.site,
        "site_name": args.site_name,
        "place": args.model,
        "device_id": args.device_id,
        "pi_host": args.host,
        "pi_port": args.port,
        "pi_user": args.user,
    }

    url = f"{DEFAULT_SERVICE}/pipeline/deploy"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            result = json.loads(res.read())
    except Exception as e:
        print(f"❌ 部署失敗: {e}")
        sys.exit(1)

    elapsed = round(time.time() - t0, 2)
    dev_id = result.get("device_id")
    schema = result.get("schema", {})
    timings = result.get("timings", {})

    if result.get("png_base64"):
        import base64
        with open(args.out_diagram, "wb") as f:
            f.write(base64.b64decode(result["png_base64"]))

    print(f"\n🎉 部署完全成功！(總耗時: {elapsed}s)")
    print(f"   • 裝置 ID: {dev_id}")
    print(f"   • AI 生成協定: {schema.get('protocol')}")
    print(f"   • AI 提取欄位: {[f['key'] for f in schema.get('fields', [])]}")
    print(f"   • 階段耗時: Qwen AI={timings.get('llm_sec')}s | SSH 部署={timings.get('deploy_sec')}s | 配線圖={timings.get('diagram_sec')}s")
    print(f"   • 實體配線圖: {args.out_diagram}")
    print("\n📋 Fritzing 接線對照表 (Wiring Table):")
    for w in result.get("wiring_table", []):
        print(f"   [ {w.get('sensor')} ] {w.get('sensor_pin')} ──> 樹莓派 {w.get('pi_pin')} ({w.get('function')})")

if __name__ == "__main__":
    main()
