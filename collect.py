#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PCS 采集器
==========

⚠️ 必须在【境外出口】运行。中国大陆 IP 会被 Cloudflare 以 IP 级规则阻断
   （实测返回 403 "Attention Required! | Cloudflare"），cloudscraper 等
   挑战求解库对此无效——那是 IP 阻断，不是 JS 挑战。

设计原则：境外只做「下载 + 结构化」，结果主动 POST 回境内接口（推送模式）。
不要做成境内去拉取境外端点：workers.dev 在国内不可达，raw.githubusercontent.com 很慢。

用法
----
    # 1) 先做出口自检（最重要，先跑这个）
    python collect.py --check

    # 2) 采单个车手，输出到本地文件
    python collect.py --task rider --slug rider/tadej-pogacar --out rider.json

    # 3) 采一整届赛事（赛事 + 出发名单 + 赛段清单，不含成绩）
    python collect.py --task race --slug race/tour-de-france/2026 --out race.json

    # 4) 采一整届赛事【含全部赛段成绩与分类榜】—— 拿成绩用这个
    python collect.py --task race-deep --slug race/tour-de-france/2026 --out race_full.json

    # 5) 只采单个赛段
    python collect.py --task stage --slug race/tour-de-france/2026/stage-1 --out s1.json

    # 6) 批量 + 推送回境内
    python collect.py --config tasks.txt \
        --push https://api.yourdomain.com/ingest/pcs \
        --secret "$INGEST_SECRET" --out batch.json

任务类型（tasks.txt 每行一条：`类型<空格或Tab>slug`，# 开头为注释）
    race-deep  race/tour-de-france/2026        # 赛事全量，含赛段成绩
    race       race/tour-de-france/2026        # 仅赛事 + 出发名单
    rider      rider/tadej-pogacar
    team       team/uae-team-emirates-xrg-2026
    stage      race/tour-de-france/2026/stage-1
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import random
from typing import Any, Dict, List, Optional

import requests

BASE = "https://www.procyclingstats.com/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------
# 限速：PCS 是免费公益站，务必克制
# --------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, rps: float = 1.0):
        self.min_interval = 1.0 / max(rps, 0.05)
        self._last = 0.0

    def wait(self):
        delta = time.time() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta + random.uniform(0, 0.15))
        self._last = time.time()


LIMITER = RateLimiter(rps=1.0)


def http_get(path: str, timeout: int = 30, retries: int = 3) -> str:
    """带重试的 GET。返回 HTML 文本。"""
    url = BASE + path.lstrip("/")
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        LIMITER.wait()
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code == 403:
                raise RuntimeError(
                    "403 —— 当前出口 IP 被 Cloudflare 阻断。"
                    "请确认脚本运行在境外节点（GitHub Actions / 境外 VPS / Workers）。"
                )
            last_err = RuntimeError(f"HTTP {r.status_code}")
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"请求失败 {url}: {last_err}")


def check_exit() -> int:
    """出口自检：确认当前网络位置能否访问 PCS。"""
    print("正在自检出口 IP ...")
    try:
        ip = requests.get("https://api.ipify.org", timeout=15).text.strip()
        geo = requests.get(
            "http://ip-api.com/json/?fields=status,country,regionName,isp,query",
            timeout=15,
        ).json()
        print(f"  出口 IP : {ip}")
        print(f"  归属地  : {geo.get('country')} / {geo.get('regionName')}")
        print(f"  ISP     : {geo.get('isp')}")
    except Exception as e:  # noqa: BLE001
        print(f"  出口信息获取失败（不影响后续）: {e}")

    try:
        html = http_get("rider/tadej-pogacar", retries=1)
        ok = "Poga" in html or "poga" in html
        print(f"  PCS 抓取: {'✅ 成功' if ok else '⚠️ 返回但内容异常'} ({len(html)} bytes)")
        return 0 if ok else 1
    except RuntimeError as e:
        print(f"  PCS 抓取: ❌ {e}")
        return 2


# --------------------------------------------------------------------------
# 采集任务
# --------------------------------------------------------------------------
def collect_rider(slug: str) -> Dict[str, Any]:
    from procyclingstats import Rider, RiderResults

    out: Dict[str, Any] = {"entity": "rider", "slug": slug, "source_url": BASE + slug}

    try:
        out["profile"] = Rider(slug).parse()
    except Exception as e:  # noqa: BLE001
        out["profile_error"] = f"{type(e).__name__}: {e}"

    LIMITER.wait()
    try:
        out["results"] = RiderResults(slug + "/results").parse()
    except Exception as e:  # noqa: BLE001
        out["results_error"] = f"{type(e).__name__}: {e}"

    return out


def collect_team(slug: str) -> Dict[str, Any]:
    from procyclingstats import Team

    out: Dict[str, Any] = {"entity": "team", "slug": slug, "source_url": BASE + slug}
    try:
        out["team"] = Team(slug).parse()
    except Exception as e:  # noqa: BLE001
        out["team_error"] = f"{type(e).__name__}: {e}"
    return out


def collect_race(slug: str, with_startlist: bool = True) -> Dict[str, Any]:
    """一届赛事：赛事本体 + 出发名单 + 赛段清单。slug 形如 race/tour-de-france/2026

    注意：PCS 的成绩数据在【赛段页】里，不在赛事总览页。
    要拿成绩请用 --task race-deep（会自动遍历所有赛段）或 --task stage。
    """
    from procyclingstats import Race, RaceStartlist

    out: Dict[str, Any] = {"entity": "race", "slug": slug, "source_url": BASE + slug}

    try:
        race = Race(slug).parse()
        out["race"] = race
    except Exception as e:  # noqa: BLE001
        out["race_error"] = f"{type(e).__name__}: {e}"
        return out

    if with_startlist:
        LIMITER.wait()
        try:
            out["startlist"] = RaceStartlist(slug + "/startlist").parse()
        except Exception as e:  # noqa: BLE001
            out["startlist_error"] = f"{type(e).__name__}: {e}"

    return out


def collect_stage(slug: str, with_classifications: bool = True) -> Dict[str, Any]:
    """单个赛段：赛段信息 + 赛段成绩 + 各分类榜。

    slug 形如 race/tour-de-france/2026/stage-1
    返回 results（赛段成绩）/ gc / points / kom / youth（各分类榜）
    """
    from procyclingstats import Stage

    out: Dict[str, Any] = {"entity": "stage", "slug": slug, "source_url": BASE + slug}
    st = Stage(slug)

    try:
        out["stage"] = st.parse()
    except Exception as e:  # noqa: BLE001
        out["stage_error"] = f"{type(e).__name__}: {e}"
        return out

    try:
        out["results"] = st.results()
    except Exception as e:  # noqa: BLE001
        out["results_error"] = f"{type(e).__name__}: {e}"

    if with_classifications:
        for key, getter in (
            ("gc", st.gc),
            ("points", st.points),
            ("kom", st.kom),
            ("youth", st.youth),
        ):
            LIMITER.wait()
            try:
                rows = getter()
                if rows:
                    out[key] = rows
            except Exception as e:  # noqa: BLE001
                out[f"{key}_error"] = f"{type(e).__name__}: {e}"

    return out


def collect_race_deep(slug: str, with_startlist: bool = True) -> Dict[str, Any]:
    """一届赛事全量：赛事 + 出发名单 + 逐赛段成绩与分类榜。

    这是拿完整成绩数据的主力任务。请求数 = 1 + 1 + 赛段数 × 1。
    21 赛段的大环赛约 23 次请求，按 1 req/s 约 25 秒。
    """
    from procyclingstats import Race, RaceStartlist

    out: Dict[str, Any] = {
        "entity": "race_deep", "slug": slug, "source_url": BASE + slug, "stages": []
    }

    try:
        race_obj = Race(slug)
        out["race"] = race_obj.parse()
    except Exception as e:  # noqa: BLE001
        out["race_error"] = f"{type(e).__name__}: {e}"
        return out

    if with_startlist:
        LIMITER.wait()
        try:
            out["startlist"] = RaceStartlist(slug + "/startlist").parse()
        except Exception as e:  # noqa: BLE001
            out["startlist_error"] = f"{type(e).__name__}: {e}"

    # 取赛段清单
    stage_urls: List[str] = []
    try:
        for row in race_obj.stages("stage_url", "stage_name", "date"):
            u = row.get("stage_url")
            if u:
                stage_urls.append(u.lstrip("/"))
    except Exception as e:  # noqa: BLE001
        out["stages_list_error"] = f"{type(e).__name__}: {e}"

    if not stage_urls:
        # 单日赛：直接抓本体作为唯一"赛段"
        stage_urls = [slug]

    out["stage_count"] = len(stage_urls)
    for i, u in enumerate(stage_urls, 1):
        LIMITER.wait()
        print(f"      赛段 {i}/{len(stage_urls)}: {u}")
        try:
            out["stages"].append(collect_stage(u))
        except RuntimeError as e:
            out["stages_fatal"] = str(e)
            break
        except Exception as e:  # noqa: BLE001
            out["stages"].append(
                {"entity": "stage", "slug": u, "fatal": f"{type(e).__name__}: {e}"}
            )

    return out


TASKS = {
    "rider": collect_rider,
    "team": collect_team,
    "race": collect_race,
    "stage": collect_stage,
    "race-deep": collect_race_deep,
}


# --------------------------------------------------------------------------
# 推送回境内（HMAC 签名）
# --------------------------------------------------------------------------
def push(items: List[Dict[str, Any]], endpoint: str, secret: str) -> bool:
    body = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    try:
        r = requests.post(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Timestamp": ts,
                "X-Signature": sig,
                "User-Agent": "pcs-collector/1.0",
            },
            timeout=60,
        )
        print(f"  推送: HTTP {r.status_code} ({len(body)} bytes)")
        return 200 <= r.status_code < 300
    except Exception as e:  # noqa: BLE001
        print(f"  推送失败: {type(e).__name__}: {e}")
        return False


def load_config(path: str) -> List[tuple]:
    pairs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace("\t", " ").split()
            if len(parts) >= 2:
                pairs.append((parts[0].lower(), parts[1]))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description="PCS 采集器（须在境外出口运行）")
    ap.add_argument("--check", action="store_true", help="只做出口自检")
    ap.add_argument("--task", choices=list(TASKS))
    ap.add_argument("--slug")
    ap.add_argument("--config", help="批量任务文件")
    ap.add_argument("--out", help="输出 JSON 文件")
    ap.add_argument("--push", help="境内接收接口 URL")
    ap.add_argument("--secret", default=os.environ.get("INGEST_SECRET", ""))
    ap.add_argument("--rps", type=float, default=1.0, help="每秒请求数，默认 1（请保持克制）")
    ap.add_argument("--batch-size", type=int, default=10, help="每批推送条数")
    args = ap.parse_args()

    global LIMITER
    LIMITER = RateLimiter(rps=args.rps)

    if args.check:
        return check_exit()

    if not args.config and not (args.task and args.slug):
        ap.error("需要 --config，或同时提供 --task 与 --slug")

    jobs = load_config(args.config) if args.config else [(args.task, args.slug)]
    items: List[Dict[str, Any]] = []

    for i, (task, slug) in enumerate(jobs, 1):
        if task not in TASKS:
            print(f"[{i}/{len(jobs)}] 跳过未知任务类型: {task}")
            continue
        print(f"[{i}/{len(jobs)}] {task} {slug}")
        try:
            data = TASKS[task](slug)
            errs = [k for k in data if k.endswith("_error")]
            print(f"    {'⚠️ 部分失败 ' + ','.join(errs) if errs else '✅ 完成'}")
        except RuntimeError as e:
            print(f"    ❌ 中断: {e}")
            break
        except Exception as e:  # noqa: BLE001
            print(f"    ❌ {type(e).__name__}: {e}")
            data = {"entity": task, "slug": slug, "fatal": f"{type(e).__name__}: {e}"}

        data["collected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        items.append(data)

        if args.push and len(items) >= args.batch_size:
            push(items, args.push, args.secret)
            items = []

    if args.push and items:
        push(items, args.push, args.secret)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        print(f"已写入 {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
