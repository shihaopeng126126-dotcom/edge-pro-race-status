#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
境内接收端示例（最小可用）
=========================

境外节点抓完会 POST 到这里。本示例只做三件事：
  1. 校验 HMAC 签名 + 时间戳（防伪造、防重放）
  2. 把原始数据落盘（原始 HTML / JSON 留痕，便于重解析）
  3. 返回 200

真实项目里，第 3 步应换成：解析 → 写 MySQL → 进异常队列。

运行：
    pip install flask
    export INGEST_SECRET='your-secret'
    python ingest_example.py        # 监听 0.0.0.0:8787
"""

import hashlib
import hmac
import json
import os
import time
from typing import Tuple

from flask import Flask, request

app = Flask(__name__)

SECRET = os.environ.get("INGEST_SECRET", "change-me").encode()
STORE_DIR = os.environ.get("STORE_DIR", "./ingested")
MAX_SKEW = 300          # 允许的时间戳偏移（秒）
os.makedirs(STORE_DIR, exist_ok=True)


def verify(raw: bytes, ts: str, sig: str) -> Tuple[bool, str]:
    if not ts or not sig:
        return False, "missing signature headers"
    try:
        if abs(time.time() - int(ts)) > MAX_SKEW:
            return False, "timestamp out of window (replay?)"
    except ValueError:
        return False, "bad timestamp"
    expected = hmac.new(SECRET, ts.encode() + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "signature mismatch"
    return True, "ok"


@app.post("/ingest/pcs")
def ingest():
    raw = request.get_data()
    ok, reason = verify(raw, request.headers.get("X-Timestamp", ""),
                        request.headers.get("X-Signature", ""))
    if not ok:
        return {"ok": False, "reason": reason}, 401

    try:
        payload = json.loads(raw)
        items = payload.get("items", [])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"bad json: {e}"}, 400

    fname = os.path.join(STORE_DIR, f"pcs_{int(time.time())}.json")
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)

    # TODO: 在这里做解析与入库
    #   parse_items(items) -> 写入 riders / races / results / ranking_snapshots
    #   解析失败的条目进人工队列，不要静默丢弃

    return {"ok": True, "received": len(items), "stored": fname}


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8787)))
