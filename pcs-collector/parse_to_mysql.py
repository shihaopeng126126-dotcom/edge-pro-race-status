#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PCS 采集结果 → MySQL 入库
=========================

把 collect.py 产出的 JSON 落进 schema_mysql8.sql 建的库里。

设计要点
--------
1. **幂等**：全部用 INSERT ... ON DUPLICATE KEY UPDATE。同一个 slug 反复采、
   反复入库不会产生重复行，重跑安全。
2. **PCS slug 直接当主表 slug**：riders.slug / teams.slug / races.slug 就是
   PCS 的 slug（如 `tadej-pogacar`），天然对齐，不依赖 external_id_map 做二次查找。
3. **软删除可复活**：UPSERT 命中已软删的行时会把 is_deleted 重置为 0，
   避免"删了又采回来"时数据缺失。
4. **来源留痕**：每张落库的记录都会在 sources 写一条来源，便于回溯与纠错。

用法
----
    # 先干跑，只看要写什么，不写库（强烈建议第一次先跑这个）
    python parse_to_mysql.py --input batch.json --dry-run

    # 真实入库
    export DB_HOST=127.0.0.1 DB_PORT=3306 DB_USER=root DB_PASS=xxx DB_NAME=cycling
    python parse_to_mysql.py --input batch.json

    # 单文件 + 打印统计
    python parse_to_mysql.py --input race_full.json --verbose

依赖
----
    pip install pymysql
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import pymysql
except ImportError:  # pragma: no cover
    print("缺少依赖：pip install pymysql", file=sys.stderr)
    sys.exit(1)


SOURCE = "pcs"
BASE_URL = "https://www.procyclingstats.com/"

# PCS 的 profile_icon（p1~p5）→ 地形。PCS 的 stage_type 只有 ITT/TTT/RR 三种，
# 平路/山地这些信息藏在 profile_icon 里，两者要结合才能落到我们的枚举。
PROFILE_MAP = {
    "p1": "flat", "p2": "flat", "p3": "hilly", "p4": "mountain", "p5": "mountain",
}
VALID_STAGE_TYPES = {
    "flat", "hilly", "mountain", "itt", "ttt", "prologue", "cobbled", "rest", "other",
}

# 三大环赛与五大古典赛（PCS 不直接给这个标记，按 slug 判断）
GRAND_TOURS = {"tour-de-france", "giro-d-italia", "vuelta-a-espana"}
MONUMENTS = {
    "milano-sanremo", "ronde-van-vlaanderen", "paris-roubaix",
    "liege-bastogne-liege", "il-lombardia",
}

# PCS 的成绩状态 → results.status 枚举
STATUS_MAP = {
    "DF": "finished", "DNF": "dnf", "DNS": "dns", "OTL": "otl", "DSQ": "dsq",
    "": "finished", None: "finished",
}

# team_seasons.level 是 ENUM，非法值会直接报错，这里白名单过滤
VALID_LEVELS = {"WT", "PRT", "CT", "WWT", "WCT", "NAT"}

# 常见国家码兜底（PCS 给的是 ISO 3166-1 alpha-2，与 nations.iso2 一致）
# 未收录的会在入库时自动建一条 name_zh 为空的记录，后续人工补中文名。


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def safe_int(val: Any, default: int = 0) -> int:
    """NOT NULL 的数值字段用：None / 空串 / 非数字统一回落到默认值。"""
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def norm_stage_type(stage_type: Any, profile_icon: Any, stage_no: str = "") -> str:
    """PCS 的 (stage_type, profile_icon) → stages.stage_type 枚举。

    PCS 只会给 ITT / TTT / RR 三种 stage_type，地形要看 profile_icon：
      p1~p2=平路  p3=丘陵  p4~p5=山地
    """
    if stage_no == "P":
        return "prologue"
    stt = (str(stage_type).strip().upper() if stage_type else "")
    if stt == "ITT":
        return "itt"
    if stt == "TTT":
        return "ttt"
    if stt == "RR" or stt == "":
        pi = (str(profile_icon).strip().lower() if profile_icon else "")
        return PROFILE_MAP.get(pi, "other")
    return stt.lower() if stt.lower() in VALID_STAGE_TYPES else "other"


def norm_status(val: Any) -> str:
    """PCS 的 DF/DNF/... → 表里的 finished/dnf/..."""
    return STATUS_MAP.get((str(val).strip().upper() if val else ""), "finished")
def to_seconds(val: Any) -> Optional[int]:
    """把 PCS 的时间字符串转成秒。

    支持：'4:12:33' / '12:33' / '+ 0:12' / '0:12' / '' / None
    """
    if val is None:
        return None
    s = str(val).strip().replace("+", "").replace("−", "").replace("-", "")
    s = s.strip()
    if not s or s in {"-", "–"}:
        return None
    parts = s.split(":")
    try:
        parts = [int(float(p)) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return None


def parse_date(val: Any) -> Optional[str]:
    """把 PCS 的日期统一成 YYYY-MM-DD。兼容多种展示格式。"""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # 已是 ISO
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return s
    # 04.07.2026 / 4.7.2026
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    # MM-DD（Race.stages() 只给月日，年份由赛事补）
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)
    if m:
        return f"__{int(m.group(1)):02d}-{int(m.group(2)):02d}"  # 后处理
    # DD.MM 欧洲简写且无年份（如 04.07），转成 __MM-DD 再按赛事年份补齐
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", s)
    if m:
        return f"__{int(m.group(2)):02d}-{int(m.group(1)):02d}"  # 后处理
    return None


def split_race_slug(slug: str) -> tuple:
    """race/tour-de-france/2026 → ('tour-de-france', 2026)"""
    parts = [p for p in slug.split("/") if p]
    # ['race', 'tour-de-france', '2026']
    if len(parts) >= 3 and parts[-1].isdigit():
        return parts[1], int(parts[-1])
    if len(parts) >= 2:
        return parts[1], None
    return slug, None


def split_team_slug(slug: str) -> tuple:
    """team/uae-team-emirates-xrg-2026 → ('uae-team-emirates-xrg', 2026)

    PCS 的车队 URL 带赛季，而我们的 teams 表不带赛季，需要拆开。
    规则：尾部形如 -YYYY 且年份在 1990–2035 之间才拆。
    """
    parts = [p for p in slug.split("/") if p]
    raw = parts[1] if len(parts) >= 2 else slug
    m = re.match(r"^(.+)-(\d{4})$", raw)
    if m:
        year = int(m.group(2))
        if 1990 <= year <= 2035:
            return m.group(1), year
    return raw, None


def rider_slug_from_url(url: Optional[str]) -> Optional[str]:
    """rider/tadej-pogacar → tadej-pogacar"""
    if not url:
        return None
    parts = [p for p in url.split("/") if p]
    return parts[1] if len(parts) >= 2 else parts[0]


def team_slug_from_url(url: Optional[str]) -> Optional[str]:
    """team/uae-team-emirates-xrg-2026 → uae-team-emirates-xrg-2026（保留赛季后缀）"""
    if not url:
        return None
    parts = [p for p in url.split("/") if p]
    return "/".join(parts) if parts else None


def stage_no_from_slug(slug: str) -> str:
    """race/tdf/2026/stage-1 → '1'；prologue → 'P'"""
    tail = [p for p in slug.split("/") if p][-1]
    m = re.search(r"stage-(\w+)", tail)
    if m:
        return m.group(1)
    if "prologue" in tail:
        return "P"
    return tail


def parse_date_strict(val: Any) -> Optional[str]:
    """只接受完整日期（YYYY-MM-DD 或 DD.MM.YYYY），否则返回 None。

    用于 race_editions.start_date 这类必须精确到年的字段——
    PCS 有些页面只给 MM-DD，与其插入半成品值，不如留空让人后期核对。
    """
    d = parse_date(val)
    if d and not d.startswith("__"):
        return d
    return None


def full_name_split(name: Optional[str]) -> tuple:
    """'Tadej Pogačar' → ('Tadej', 'Pogačar')"""
    if not name:
        return None, None
    n = re.sub(r"\s+", " ", name).strip()
    if " " in n:
        first, last = n.split(" ", 1)
        return first, last
    return None, n


# ---------------------------------------------------------------------------
# 入库器
# ---------------------------------------------------------------------------
class Writer:
    def __init__(self, conn, dry_run: bool = False, verbose: bool = False):
        self.conn = conn
        self.dry = dry_run
        self.verbose = verbose
        self.stats: Dict[str, int] = {}
        # 内存缓存，避免重复查库
        self._nation_cache: Dict[str, int] = {}
        self._rider_cache: Dict[str, int] = {}
        self._team_cache: Dict[str, int] = {}
        self._team_season_cache: Dict[tuple, int] = {}
        self._race_cache: Dict[str, int] = {}
        self._edition_cache: Dict[tuple, int] = {}
        self._stage_cache: Dict[tuple, int] = {}

    # -- 底层 ---------------------------------------------------------------
    def _exec(self, sql: str, args: tuple = ()) -> Optional[int]:
        """执行 SQL。返回 lastrowid（插入）或已有 id（更新）。"""
        self.stats["sql"] = self.stats.get("sql", 0) + 1
        if self.dry:
            return None
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            if cur.lastrowid and cur.lastrowid != 0:
                return cur.lastrowid
            return None

    def _q1(self, sql: str, args: tuple = ()) -> Optional[tuple]:
        if self.dry:
            return None
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()

    def bump(self, key: str, n: int = 1):
        self.stats[key] = self.stats.get(key, 0) + n

    # -- 基础实体 -----------------------------------------------------------
    def nation_id(self, iso2: Optional[str]) -> Optional[int]:
        if not iso2:
            return None
        iso2 = iso2.upper()[:2]
        if iso2 in self._nation_cache:
            return self._nation_cache[iso2]
        row = self._q1("SELECT id FROM nations WHERE iso2=%s AND is_deleted=0", (iso2,))
        if row:
            self._nation_cache[iso2] = row[0]
            return row[0]
        nid = self._exec(
            "INSERT INTO nations (iso2, name_en) VALUES (%s, %s)", (iso2, iso2)
        )
        if nid is None:
            nid = (self._q1("SELECT id FROM nations WHERE iso2=%s", (iso2,)) or (None,))[0]
        if nid:
            self._nation_cache[iso2] = nid
        self.bump("nations")
        return nid

    def rider_id(self, slug: str, name: Optional[str] = None,
                 nationality: Optional[str] = None) -> Optional[int]:
        """按 slug 取车手 id；不存在则建一条最小记录。"""
        if not slug:
            return None
        if slug in self._rider_cache:
            return self._rider_cache[slug]
        row = self._q1("SELECT id FROM riders WHERE slug=%s AND is_deleted=0", (slug,))
        if row:
            self._rider_cache[slug] = row[0]
            return row[0]
        first, last = full_name_split(name)
        rid = self._exec(
            """INSERT INTO riders (slug, name_en, first_name, last_name, nation_id)
               VALUES (%s, %s, %s, %s, %s)""",
            (slug, name, first, last, self.nation_id(nationality)),
        )
        if rid is None:
            rid = (self._q1("SELECT id FROM riders WHERE slug=%s", (slug,)) or (None,))[0]
        if rid:
            self._rider_cache[slug] = rid
        self.bump("riders")
        return rid

    def team_season_id(self, team_url: Optional[str],
                       team_name: Optional[str] = None) -> Optional[int]:
        """team/uae-team-emirates-xrg-2026 → team_seasons.id

        车队在成绩表里是以"带赛季的 URL"出现的，所以这里直接落到 team_seasons，
        同时保证 teams 母表存在。
        """
        if not team_url:
            return None
        full = team_slug_from_url(team_url)          # uae-team-emirates-xrg-2026
        if not full:
            return None
        base, season = split_team_slug(full)         # base + 2026
        if season is None:
            return None
        key = (base, season)
        if key in self._team_season_cache:
            return self._team_season_cache[key]

        # teams 母表
        tid = self._team_cache.get(base)
        if tid is None:
            row = self._q1("SELECT id FROM teams WHERE slug=%s AND is_deleted=0", (base,))
            if row:
                tid = row[0]
            else:
                tid = self._exec(
                    "INSERT INTO teams (slug, name_en) VALUES (%s, %s)",
                    (base, team_name or base),
                )
                if tid is None:
                    tid = (self._q1("SELECT id FROM teams WHERE slug=%s", (base,)) or (None,))[0]
                self.bump("teams")
            if tid:
                self._team_cache[base] = tid

        # team_seasons
        row = self._q1(
            "SELECT id FROM team_seasons WHERE team_id=%s AND season=%s AND is_deleted=0",
            (tid, season),
        )
        if row:
            tsid = row[0]
        else:
            tsid = self._exec(
                """INSERT INTO team_seasons (team_id, season, name_en)
                   VALUES (%s, %s, %s)""",
                (tid, season, team_name or base),
            )
            if tsid is None:
                tsid = (self._q1(
                    "SELECT id FROM team_seasons WHERE team_id=%s AND season=%s",
                    (tid, season)) or (None,))[0]
            self.bump("team_seasons")
        if tsid:
            self._team_season_cache[key] = tsid
        return tsid

    def race_id(self, slug: str, name: Optional[str] = None) -> Optional[int]:
        if not slug:
            return None
        if slug in self._race_cache:
            return self._race_cache[slug]
        row = self._q1("SELECT id FROM races WHERE slug=%s AND is_deleted=0", (slug,))
        if row:
            self._race_cache[slug] = row[0]
            return row[0]
        rid = self._exec(
            """INSERT INTO races (slug, name_en, is_grand_tour, is_monument)
               VALUES (%s, %s, %s, %s)""",
            (slug, name or slug, 1 if slug in GRAND_TOURS else 0,
             1 if slug in MONUMENTS else 0),
        )
        if rid is None:
            rid = (self._q1("SELECT id FROM races WHERE slug=%s", (slug,)) or (None,))[0]
        if rid:
            self._race_cache[slug] = rid
        self.bump("races")
        return rid

    def edition_id(self, race_slug: str, year: int, name: Optional[str] = None) -> Optional[int]:
        if not race_slug or not year:
            return None
        rid = self.race_id(race_slug, name)
        key = (rid, year)
        if key in self._edition_cache:
            return self._edition_cache[key]
        row = self._q1(
            "SELECT id FROM race_editions WHERE race_id=%s AND year=%s AND is_deleted=0",
            (rid, year),
        )
        if row:
            eid = row[0]
        else:
            eid = self._exec(
                """INSERT INTO race_editions (race_id, year, season, name_en)
                   VALUES (%s, %s, %s, %s)""",
                (rid, year, year, f"{name or race_slug} {year}"),
            )
            if eid is None:
                eid = (self._q1(
                    "SELECT id FROM race_editions WHERE race_id=%s AND year=%s",
                    (rid, year)) or (None,))[0]
            self.bump("race_editions")
        if eid:
            self._edition_cache[key] = eid
        return eid

    def source(self, entity_type: str, entity_id: Optional[int], url: Optional[str]):
        """来源留痕。

        sources 表只有主键、没有业务唯一索引，ON DUPLICATE KEY 不会生效，
        所以这里先查后插，避免每次采集都堆积一条重复来源。
        """
        if not entity_id or not url:
            return
        row = self._q1(
            """SELECT id FROM sources
               WHERE entity_type=%s AND entity_id=%s AND source_type=%s AND source_url=%s
               LIMIT 1""",
            (entity_type, entity_id, SOURCE, url),
        )
        if row:
            return
        self._exec(
            """INSERT INTO sources (entity_type, entity_id, source_type, source_url, crawled_at)
               VALUES (%s, %s, %s, %s, NOW())""",
            (entity_type, entity_id, SOURCE, url),
        )
        self.bump("sources")

    # -- 各类实体 -----------------------------------------------------------
    def write_rider(self, item: Dict[str, Any]):
        slug = rider_slug_from_url(item.get("slug")) or item.get("slug")
        if not slug:
            return
        p = item.get("profile") or {}
        name = p.get("name")
        nat = p.get("nationality")
        birth = parse_date(p.get("birthdate"))
        height_cm = None
        if p.get("height"):
            try:
                height_cm = int(round(float(p["height"]) * 100))
            except (TypeError, ValueError):
                pass
        weight = None
        if p.get("weight"):
            try:
                weight = int(round(float(p["weight"])))
            except (TypeError, ValueError):
                pass
        sp = p.get("points_per_speciality") or {}

        self._exec(
            """INSERT INTO riders
               (slug, name_en, first_name, last_name, nation_id, birth_date,
                height_cm, weight_kg, sp_oneday, sp_gc, sp_tt, sp_sprint,
                sp_climber, sp_hills)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
                 name_en=VALUES(name_en), first_name=VALUES(first_name),
                 last_name=VALUES(last_name), nation_id=VALUES(nation_id),
                 birth_date=VALUES(birth_date), height_cm=VALUES(height_cm),
                 weight_kg=VALUES(weight_kg), sp_oneday=VALUES(sp_oneday),
                 sp_gc=VALUES(sp_gc), sp_tt=VALUES(sp_tt),
                 sp_sprint=VALUES(sp_sprint), sp_climber=VALUES(sp_climber),
                 sp_hills=VALUES(sp_hills), is_deleted=0""",
            (
                slug, name, *full_name_split(name), self.nation_id(nat), birth,
                height_cm, weight,
                sp.get("one_day_races"), sp.get("gc"), sp.get("time_trial"),
                sp.get("sprint"), sp.get("climber"), sp.get("hills"),
            ),
        )
        rid = self.rider_id(slug, name, nat)
        self.source("rider", rid, item.get("source_url"))

        # 英文原名进译名库（中文名后续人工补，或接翻译批量生成）
        if rid and name:
            self._exec(
                """INSERT INTO rider_names (rider_id, lang, name, is_primary, source)
                   VALUES (%s,'en',%s,1,%s)
                   ON DUPLICATE KEY UPDATE is_deleted=0""",
                (rid, name, SOURCE),
            )
        self.bump("rider_items")

    def write_team(self, item: Dict[str, Any]):
        full = item.get("slug") or ""
        base, season = split_team_slug(full.replace("team/", "", 1))
        t = item.get("team") or {}
        if season is None:
            return

        # teams 母表
        self._exec(
            """INSERT INTO teams (slug, name_en, license_country)
               VALUES (%s,%s,%s)
               ON DUPLICATE KEY UPDATE name_en=VALUES(name_en),
                 license_country=VALUES(license_country), is_deleted=0""",
            (base, t.get("name") or base, self._iso_of(t.get("license_country"))),
        )
        row = self._q1("SELECT id FROM teams WHERE slug=%s", (base,))
        tid = row[0] if row else None

        # team_seasons
        # level 是 ENUM，非法值会让整条 INSERT 报错，先过白名单；
        # pcs_points 是 NOT NULL DEFAULT 0，None 必须转成 0。
        level = (t.get("status") or "").strip().upper()
        if level not in VALID_LEVELS:
            level = "CT"
        self._exec(
            """INSERT INTO team_seasons
               (team_id, season, category, name_en, short_name, uci_code, level, pcs_points)
               VALUES (%s,%s,'men',%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE name_en=VALUES(name_en),
                 short_name=VALUES(short_name), uci_code=VALUES(uci_code),
                 level=VALUES(level), pcs_points=VALUES(pcs_points), is_deleted=0""",
            (tid, season, t.get("name") or base, t.get("abbreviation"),
             t.get("abbreviation"), level, safe_int(t.get("pcs_points"))),
        )
        row = self._q1(
            "SELECT id FROM team_seasons WHERE team_id=%s AND season=%s AND category='men'",
            (tid, season))
        tsid = row[0] if row else None
        self.source("team_season", tsid, item.get("source_url"))

        # 阵容 → rider_team_contracts
        for r in (t.get("riders") or []):
            rslug = rider_slug_from_url(r.get("rider_url"))
            if not rslug:
                continue
            rid = self.rider_id(rslug, r.get("rider_name"), r.get("nationality"))
            if not rid or not tsid:
                continue
            self._exec(
                """INSERT INTO rider_team_contracts
                   (rider_id, team_season_id, season, start_date, end_date)
                   VALUES (%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE end_date=VALUES(end_date), is_deleted=0""",
                (rid, tsid, season,
                 f"{season}-01-01" if season else None,
                 f"{season}-12-31" if season else None),
            )
            self.bump("contracts")
        self.bump("team_items")

    def _iso_of(self, s: Optional[str]) -> Optional[str]:
        """license_country 可能是英文名，这里只在确实是 2 位码时才返回。"""
        if s and len(s.strip()) == 2 and s.strip().isalpha():
            return s.strip().upper()
        return None

    def write_race(self, item: Dict[str, Any], deep_stages: List[Dict[str, Any]] = None):
        race_slug, year = split_race_slug(item.get("slug") or "")
        if not race_slug or not year:
            return
        r = item.get("race") or {}

        # races
        self._exec(
            """INSERT INTO races (slug, name_en, country, is_grand_tour, is_monument)
               VALUES (%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE name_en=VALUES(name_en),
                 country=VALUES(country), is_deleted=0""",
            (race_slug, r.get("name") or race_slug, (r.get("nationality") or "").upper()[:2] or None,
             1 if race_slug in GRAND_TOURS else 0, 1 if race_slug in MONUMENTS else 0),
        )

        # race_editions
        # ⚠️ 两个易错点：
        #   1) race_editions.category 是【赛制】枚举(oneday/stage/other)，
        #      而 PCS 的 category 是【组别】("Men Elite")，直接塞进去会
        #      Data truncated 报错。赛制要用 is_one_day_race 判断。
        #   2) 日期用严格解析：某些页面只给 MM-DD，与其写半成品不如留空。
        rid = self.race_id(race_slug, r.get("name"))
        race_category = "oneday" if r.get("is_one_day_race") else "stage"
        self._exec(
            """INSERT INTO race_editions
               (race_id, year, season, edition_no, name_en, uci_class, category,
                start_date, end_date, stage_count, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE edition_no=VALUES(edition_no),
                 name_en=VALUES(name_en), uci_class=VALUES(uci_class),
                 category=VALUES(category), start_date=VALUES(start_date),
                 end_date=VALUES(end_date), stage_count=VALUES(stage_count),
                 status=VALUES(status), is_deleted=0""",
            (
                rid, year, year, r.get("edition"),
                f"{r.get('name') or race_slug} {year}",
                r.get("uci_tour"), race_category,
                parse_date_strict(r.get("startdate")),
                parse_date_strict(r.get("enddate")),
                len(r.get("stages") or deep_stages or []),
                "finished",
            ),
        )
        eid = self.edition_id(race_slug, year, r.get("name"))
        self.source("race_edition", eid, item.get("source_url"))

        # 出发名单
        for s in (item.get("startlist") or {}).get("startlist", []):
            rslug = rider_slug_from_url(s.get("rider_url"))
            if not rslug:
                continue
            rid = self.rider_id(rslug, s.get("rider_name"), s.get("nationality"))
            tsid = self.team_season_id(s.get("team_url"), s.get("team_name"))
            if not rid or not eid:
                continue
            self._exec(
                """INSERT INTO startlist_entries (edition_id, rider_id, team_season_id, bib)
                   VALUES (%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE team_season_id=VALUES(team_season_id),
                     bib=VALUES(bib), is_deleted=0""",
                (eid, rid, tsid, str(s.get("rider_number")) if s.get("rider_number") else None),
            )
            self.bump("startlist")

        # 逐赛段
        for st in (deep_stages or []):
            self.write_stage(st, race_slug, year, eid)
        self.bump("race_items")

    def write_stage(self, item: Dict[str, Any], race_slug: str,
                    year: int, edition_id: Optional[int]):
        slug = item.get("slug") or ""
        st = item.get("stage") or {}
        stage_no = stage_no_from_slug(slug)
        try:
            seq = int(stage_no)
        except (TypeError, ValueError):
            seq = 999 if stage_no == "P" else 0

        # 赛段日期：PCS 可能只给 MM-DD，这里用赛事年份补齐
        d = parse_date(st.get("date"))
        if d and d.startswith("__"):
            d = f"{year}-{d[2:]}"
        elif d is None and year:
            d = None

        # stages
        self._exec(
            """INSERT INTO stages
               (edition_id, stage_no, stage_seq, date_local, start_city, finish_city,
                distance_km, elevation_gain_m, stage_type, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE date_local=VALUES(date_local),
                 start_city=VALUES(start_city), finish_city=VALUES(finish_city),
                 distance_km=VALUES(distance_km),
                 elevation_gain_m=VALUES(elevation_gain_m),
                 stage_type=VALUES(stage_type), is_deleted=0""",
            (
                edition_id, stage_no, seq, d,
                st.get("departure"), st.get("arrival"),
                st.get("distance"), st.get("vertical_meters"),
                norm_stage_type(st.get("stage_type"), st.get("profile_icon"), stage_no),
                "finished",
            ),
        )
        row = self._q1(
            "SELECT id FROM stages WHERE edition_id=%s AND stage_no=%s", (edition_id, stage_no))
        sid = row[0] if row else None
        self.source("stage", sid, item.get("source_url"))
        if not sid:
            self.bump("stage_missing")
            return

        # 赛段成绩
        # 冠军用时是计算 timelag 的基准：优先取 rank=1 那行，取不到就退回第一行。
        rows = item.get("results") or []
        winner_sec: Optional[int] = None
        for r in rows:
            if (r.get("rank") or 0) == 1:
                winner_sec = to_seconds(r.get("time"))
                break
        if winner_sec is None and rows:
            winner_sec = to_seconds(rows[0].get("time"))

        for r in rows:
            rslug = rider_slug_from_url(r.get("rider_url"))
            if not rslug:
                continue
            rid = self.rider_id(rslug, r.get("rider_name"), r.get("nationality"))
            tsid = self.team_season_id(r.get("team_url"), r.get("team_name"))
            if not rid:
                continue
            sec = to_seconds(r.get("time"))
            self._exec(
                """INSERT INTO results
                   (stage_id, rider_id, team_season_id, rank_no, time_seconds,
                    timelag_seconds, bonus_seconds, uci_points, pcs_points,
                    status, source_type, source_url, crawled_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                   ON DUPLICATE KEY UPDATE rank_no=VALUES(rank_no),
                     time_seconds=VALUES(time_seconds),
                     timelag_seconds=VALUES(timelag_seconds),
                     bonus_seconds=VALUES(bonus_seconds),
                     uci_points=VALUES(uci_points), pcs_points=VALUES(pcs_points),
                     status=VALUES(status), crawled_at=NOW(), is_deleted=0""",
                (
                    sid, rid, tsid, r.get("rank"), sec,
                    (sec - winner_sec) if (sec is not None and winner_sec is not None) else None,
                    to_seconds(r.get("bonus")),
                    safe_int(r.get("uci_points")), safe_int(r.get("pcs_points")),
                    norm_status(r.get("status")), SOURCE, item.get("source_url"),
                ),
            )
            self.bump("results")

        # 分类榜：gc / points / kom / youth
        #
        # ⚠️ GC 表的 time 列有混合语义：第一名是「累计总时间」，其余人是「+差距」。
        #    不区分的话，第二名会被存成 163 秒（其实是 +2:43），数据全错。
        #    这里先取第一名的总时间做基准，再把 "+差" 还原成绝对总时间。
        for ctype in ("gc", "points", "kom", "youth"):
            rows_c = item.get(ctype) or []

            lead_sec: Optional[int] = None
            if ctype == "gc":
                for r in rows_c:
                    if (r.get("rank") or 0) == 1:
                        raw = str(r.get("time") or "")
                        if not raw.strip().startswith("+"):
                            lead_sec = to_seconds(raw)
                        break

            for r in rows_c:
                rank = r.get("rank")
                if rank is None:          # rank_no 是 NOT NULL，没名次的行跳过
                    continue
                rslug = rider_slug_from_url(r.get("rider_url"))
                if not rslug:
                    continue
                rid = self.rider_id(rslug, r.get("rider_name"), r.get("nationality"))
                tsid = self.team_season_id(r.get("team_url"), r.get("team_name"))
                if not rid:
                    continue

                if ctype == "gc":
                    raw = str(r.get("time") or "").strip()
                    if raw.startswith("+"):
                        lag = to_seconds(raw)
                        sec = (lead_sec + lag) if (lead_sec is not None and lag is not None) else None
                    else:
                        sec = to_seconds(raw)
                    lag_val = (sec - lead_sec) if (sec is not None and lead_sec is not None) else None
                    pts = None
                else:
                    sec = None
                    lag_val = None
                    pts = safe_int(r.get("pcs_points") if r.get("pcs_points") is not None
                                   else r.get("uci_points"))

                self._exec(
                    """INSERT INTO classifications
                       (edition_id, stage_id, type, rank_no, rider_id, team_season_id,
                        value_seconds, value_points, timelag_seconds, is_leader)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE rider_id=VALUES(rider_id),
                         team_season_id=VALUES(team_season_id),
                         value_seconds=VALUES(value_seconds),
                         value_points=VALUES(value_points),
                         timelag_seconds=VALUES(timelag_seconds),
                         is_leader=VALUES(is_leader), is_deleted=0""",
                    (
                        edition_id, sid, ctype, rank, rid, tsid,
                        sec, pts, lag_val, 1 if rank == 1 else 0,
                    ),
                )
                self.bump(f"cls_{ctype}")
        self.bump("stage_items")

    # -- 分派 ---------------------------------------------------------------
    def handle(self, item: Dict[str, Any]):
        ent = item.get("entity")
        try:
            if ent == "rider":
                self.write_rider(item)
            elif ent == "team":
                self.write_team(item)
            elif ent == "race":
                self.write_race(item)
            elif ent == "race_deep":
                self.write_race(item, item.get("stages") or [])
            elif ent == "stage":
                # 单独采的赛段：从 slug 推断赛事与年份
                parts = [p for p in (item.get("slug") or "").split("/") if p]
                if len(parts) >= 3:
                    rs, yr = split_race_slug("/".join(parts[:3]))
                    eid = self.edition_id(rs, yr, rs)
                    self.write_stage(item, rs, yr, eid)
            if not self.dry:
                self.conn.commit()
        except Exception as e:  # noqa: BLE001
            if not self.dry:
                self.conn.rollback()
            self.bump("errors")
            print(f"  ❌ {ent} {item.get('slug')}: {type(e).__name__}: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="PCS 采集结果入库")
    ap.add_argument("--input", required=True, help="collect.py 产出的 JSON 文件")
    ap.add_argument("--dry-run", action="store_true", help="只解析不写库")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--host", default=os.environ.get("DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DB_PORT", "3306")))
    ap.add_argument("--user", default=os.environ.get("DB_USER", "root"))
    ap.add_argument("--password", default=os.environ.get("DB_PASS", ""))
    ap.add_argument("--db", default=os.environ.get("DB_NAME", "cycling"))
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        items = json.load(f)
    if isinstance(items, dict):
        items = [items]
    print(f"读取 {len(items)} 条采集记录（{'干跑' if args.dry_run else '写库'}）")

    # 注意：这里必须用默认的 tuple cursor —— 代码里统一用 row[0] 取 id，
    # 若改成 DictCursor 会 KeyError。
    conn = pymysql.connect(
        host=args.host, port=args.port, user=args.user, password=args.password,
        database=args.db, charset="utf8mb4", autocommit=False,
    )
    w = Writer(conn, dry_run=args.dry_run, verbose=args.verbose)
    for i, item in enumerate(items, 1):
        if args.verbose:
            print(f"[{i}/{len(items)}] {item.get('entity')} {item.get('slug')}")
        w.handle(item)

    print("\n=== 入库统计 ===")
    for k in sorted(w.stats):
        print(f"  {k:<16} {w.stats[k]}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
