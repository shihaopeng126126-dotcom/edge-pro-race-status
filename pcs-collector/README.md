# PCS 采集器

在**国内**拿到 ProCyclingStats 数据的可执行方案。

---

## 一、先认清一件事：拦截发生在两个独立维度

想抓 PCS，必须**同时**通过两道门，缺一不可：

| 维度 | 检查内容 | 失败表现 | 解法 |
|---|---|---|---|
| **① IP / ASN** | 国家地区 + 是否机房 IP | `403 Attention Required! \| Cloudflare` | **换出口 IP**：住宅代理 / 家宽 VPS |
| **② TLS 指纹** | JA3/JA4 握手特征是否像浏览器 | 同上，但仅发生在境外出口 | **换传输层**：`curl_cffi` |

**中国大陆出口两道都过不去**（实测：即便用 curl_cffi 完美伪装 Chrome131 指纹，仍 403），所以那段折腾是徒劳的：

| 尝试 | 实测结果 |
|---|---|
| 换 UA / IPv6 / 本机代理 | 均 403，响应头带 `cf-mitigated` |
| `cloudscraper`（解 CF JS 挑战） | **403 `Attention Required! \| Cloudflare`** ← 决定性证据：这是 IP 阻断，挑战求解库解不了 |
| `r.jina.ai` | 国内连接超时（服务本身不可达） |
| Wayback / archive.org | 国内连接超时 |
| allorigins / codetabs / corsproxy | 不通或 403 |

| `Python requests`（原生 OpenSSL 指纹） | 403 —— 已在 GitHub Actions（美国 Azure 出口）复现 |
| `curl_cffi`（伪装 Chrome131 指纹） | 国内仍 403（IP 维度不过）；境外出口待验证 |

> ⚠️ 修正一处旧结论：之前写「cloudscraper 返回 403 ⇒ 纯 IP 阻断 ⇒ 软件无解」，这个推论**不准确**——cloudscraper 底层仍是 requests/OpenSSL，指纹根本没换。准确说法是：**换 IP 解决维度①，换 curl_cffi 解决维度②，两条要叠加。**

所以本方案的本质是：
> 代码在国内写、在国内调度，**执行落在境外**，且**必须用浏览器 TLS 指纹**，结果**主动推回**境内。

---

## 二、传输层：curl_cffi（2026-10 实测新增）

本采集器默认用 `curl_cffi` 发请求，并把 `procyclingstats` 内部的 Session 一并替换掉：

```python
# collect.py 内部逻辑，参照即可
install_transport(impersonate="chrome131", proxy=None)
```

- `--transport auto|curl_cffi|requests`：默认 `auto`（有 curl_cffi 就用）
- `--impersonate chrome131|safari18_0|chrome110`：指纹预设，被针对时轮换试试
- `--proxy http://user:pass@host:port` 或设 `HTTPS_PROXY` 环境变量

**别装 cloudscraper**：它被 `procyclingstats` 优先使用，但底层还是 requests，会覆盖本采集器注入的传输层。

---

## 三、五条出口怎么选

| 方案 | 出口 | 成本 | 实测状态 |
|---|---|---|---|
| **GitHub Actions** | Azure 美国 | 免费 | ⚠️ 已实测：原生 requests 被 403。**curl_cffi 是否有效仍待验证**——若可通过，它就是零成本最优解 |
| **住宅代理 + 任意出口** | 真实家庭宽带 ISP | ¥10–15/GB，本项目约 ¥1–3/月 | ✅ **最可能通过**，维度①②同时满足 |
| **境外 VPS** | 自选（美国/德国/荷兰） | ¥30–50/月 | ❌ 机房 ASN，通常同样被 CF 按 IP 信誉拒；**不如住宅代理** |
| Cloudflare Workers | CF 全球边缘 | 免费（10 万 req/天） | 轻量增量；`workers.dev` 国内不可达，只能推送不能拉取 |
| 商业抓取 API（ScraperAPI / ZenRows / Apify） | 代理池 + 反爬 | 按量付费 | 不想运维时选，但成本高 |

> ⚠️ 「境外 VPS」在早期文档里被当作主力推荐，**这个说法要改**：VPS 的 IP 属于机房 ASN（DigitalOcean/Hetzner/Vultr 等），Cloudflare 会按 IP 信誉整体标记，买了大概率照样 403。**机房 IP 换住宅 IP 是关键变量，不是换国家。**

**推荐路径**：① 先在 Actions 上验证 curl_cffi 是否通（零成本）→ ② 不通则买住宅代理（先拿免费测试流量跑 `--check`）→ ③ 通过后再决定要不要配 ±30 元的家宽 VPS 长期跑。

---

## 四、快速开始（GitHub Actions，零成本）

1. **Fork / 建仓库**，把本目录放进去。
2. **境内先起好接收端**：
   ```bash
   pip install flask
   export INGEST_SECRET='换成你的随机串'
   python ingest_example.py        # 监听 :8787，需公网可达（或用内网穿透临时验证）
   ```
3. **仓库 Settings → Secrets 添加**：
   - `INGEST_URL` = `https://你的域名/ingest/pcs`
   - `INGEST_SECRET` = 与境内一致
4. **Actions 页面手动触发** `PCS Collector`，先跑 `check_only=true`。
5. 看自检日志里的**出口归属地**——必须是境外，且 PCS 抓取显示 ✅。
6. 再跑一次完整采集，看境内是否收到数据。

自检输出示例（成功时）：
```
  出口 IP : 20.xx.xx.xx
  归属地  : United States / Virginia
  ISP     : Microsoft Corporation
  PCS 抓取: ✅ 成功 (284113 bytes)
```

---

## 五、排查阶梯：403 了怎么办

按顺序做，每步都能排除一类原因：

| 步骤 | 操作 | 通过意味着什么 | 不通过怎么办 |
|---|---|---|---|
| **1** | `python collect.py --check`（在目标出口跑） | 看【1】确认出口国家和 ISP | 若 ISP 含 Microsoft/Azure/AWS/DO/Hetzner → 机房 IP，下一步 |
| **2** | 看【2】curl_cffi 是否成功 | TLS 指纹问题已解决（默认已换 curl_cffi） | 若三种指纹全 403 → 维度①不过，看下一步 |
| **3** | 试 `--impersonate safari18_0` | 某些站位按浏览器版本封 | 仍 403 → 换 IP 类型 |
| **4** | **住宅代理**：`--proxy http://user:pass@host:port` 后重跑 `--check` | IP/ASN 维度通过，可正式采集 | 仍 403 → 换代理商 / 换国家（优先美国、德国、荷兰） |
| **5** | 家宽 VPS（非机房） | IP 归属为 ISP | 放弃 PCS，改用 UCI 官方 + cqranking 等境内可达源 |

**排除法的关键判断**：同一个脚本，出口从「Azure 机房」换成「住宅宽带」后若通过，就证明之前是 IP/ASN 被封，与代码无关。

### 住宅代理选型

国内可买、支持境外住宅 IP 的服务商参考（2026 年公开报价，务必以官网实时价为准）：

| 类型 | 参考价格 | 说明 |
|---|---|---|
| 动态住宅（按流量） | ¥10–15/GB 起 | **本项目推荐**。一个赛季约 2000 请求 ≈ 100 MB，月成本约 ¥1–3 |
| 隧道代理（包月） | ¥100–500/月 含数 GB | 自动轮换 IP，适合批量 |
| 静态住宅（按 IP） | ¥10–20/月/IP | 固定 IP，稳定性最好 |

选购要点：① 要**按流量计费**的套餐，别买包月不限量（用不上）；② 国家选**美国 / 德国 / 荷兰**，别选香港、新加坡（和大陆常被同一批规则覆盖）；③ 先拿免费测试流量跑 `--check` 验证再付款。

---

## 六、三个实测出来的坑

1. **`*.workers.dev` 在国内不可达**（连接超时）。用 Workers 就只能做「worker 主动 POST 到境内」，**不能**做「境内请求 worker URL」。
2. **`raw.githubusercontent.com` 可达但很慢**（9.3s 才返回 301）。Actions 别靠「境内拉产物」回传，同样走推送。
3. **`procyclingstats` 必须锁 `selectolax<0.4`**：`selectolax>=1.0` 移除了 modest 后端，会直接 `ImportError`。已写在 `requirements.txt`。

另外：`procyclingstats` 包内部请求**没有超时设置**，网络异常时会挂住。`collect.py` 外面套了自己的 `http_get`（带超时重试 + 限速），但走包内部的方法时建议用 Actions 的 `timeout-minutes` 兜底。

---

## 七、架构：为什么是推送不是拉取

```
境外（Actions / VPS / Workers）
    下载 HTML ──→ 结构化 ──→ POST(HMAC 签名) ──→ 境内 API ──→ MySQL
                                                      ↑
                                          境内永不主动访问境外端点
```

- 境外 Serverless 域名在国内不一定可达（坑 1、2）→ 拉取会失败
- 推送可重试、可鉴权、可限流，控制权在境内

**Workers 版额外建议：境外只下载 HTML，解析放境内。** CPU 消耗几乎为零，免费额度绰绰有余；解析逻辑改了也不用重新部署境外。见 `worker.js`。

---

## 八、合规（务必读）

读 PCS 的 `robots.txt` 后有个**关键区分**：

- `User-agent: *` → **`Allow: /`**，`Content-Signal: search=yes, ai-train=no`
- 被 `Disallow` 的全是 **AI 训练 / AI Agent 爬虫**（GPTBot、ClaudeBot、CCBot、Baiduspider…）

→ **普通爬虫是允许的，我们的场景不属于 AI 训练。**

但 PCS **服务条款**写明：内容未经**书面许可**不得复制、下载、分发。商业化再分发属于灰区。建议：

1. **商业化前发邮件申请数据授权**（ToS 中留有联系邮箱）——小体量产品常能谈成免费或低价，一步消除最大风险。
2. **主源保持 UCI 官方**，PCS 用于补全历史与交叉校验，避免单点依赖。
3. **限速**：PCS 是免费公益站。默认 `--rps 1`（每秒 1 请求），别调高；做缓存，别重复抓同一页。
4. **标注数据来源**，不照搬其自建积分的算法文本，自建规则。

---

## 九、文件说明

| 文件 | 用途 |
|---|---|
| `collect.py` | 主采集器（境外运行），含出口自检；支持 rider / team / race / stage / race-deep |
| `parse_to_mysql.py` | 入库脚本（境内运行），采集 JSON → MySQL，幂等 |
| `sample_collect.json` | 样例采集结果，可直接拿来试入库 |
| `tasks.txt` | 批量任务清单 |
| `.github/workflows/pcs-collect.yml` | GitHub Actions 定时配置 |
| `worker.js` | Cloudflare Workers 轻量版（只下载不解析） |
| `ingest_example.py` | 境内接收端最小实现（HMAC 校验 + 落盘） |
| `requirements.txt` | 依赖（已锁 selectolax 版本） |

---

## 十、采集到入库的完整链路

```
境外节点                                     境内
─────────                                   ──────
collect.py --task race-deep
  下载 HTML → 结构化 → batch.json
        │
        ├─（回传：见下方三种方式）
        ▼
   batch.json  ──────────────────────→  parse_to_mysql.py  →  MySQL
```

**三种回传方式**（推荐第一种，最简单）：

1. **文件传输**（M0 验证首选）：境外跑完把 `batch.json` 取回境内，再跑入库。
   Actions 用 artifact 下载，VPS 用 `scp`。
2. **POST 推送**：境内起 `ingest_example.py`，境外 `--push https://你的域名/ingest/pcs`。
3. **境外直连境内 MySQL**：需要在 MySQL 开公网 + IP 白名单。方便但有暴露风险，
   务必只放行采集节点 IP、用独立账号、只给 `cycling` 库权限。

**先干跑再真跑**：

```bash
python parse_to_mysql.py --input batch.json --dry-run --verbose
python parse_to_mysql.py --input batch.json
```

`parse_to_mysql.py` 全表用 `INSERT ... ON DUPLICATE KEY UPDATE`，
**重复跑不会产生重复行**，可以放心重跑。

---

## 十一、采集顺序建议

数据之间有依赖，按这个顺序跑最省事：

1. **`race-deep`**（赛事全量）—— 建立赛事 / 赛段 / 成绩骨架，
   顺带把出场车手以最小信息写入 `riders`（占位，只有名字和国籍）
2. **`team`**（车队赛季）—— 补车队、阵容、`rider_team_contracts`
3. **`rider`**（车手详情）—— 补全生日、身高体重、六维能力值

第 3 步最耗时（每个车手 2 个请求）。如果只做赛事浏览，可以只跑到第 1、2 步。

---

## 十二、请求量估算（@ 1 req/s）

| 任务 | 请求数 | 耗时 |
|---|---|---|
| 一场大环赛 `race-deep` | 1 + 1 + 21 ≈ 23 | ~25 秒 |
| 一个赛季 WorldTour（约 35 场） | ~800 | ~15 分钟 |
| 一个赛季车手（约 550 人 × 2） | ~1100 | ~20 分钟 |
| 一个赛季车队（约 20 队） | ~20 | 半分钟 |
| **一个赛季全量** | **~2000** | **~35 分钟** |

历史 10 年 ≈ 10 倍 ≈ 6 小时，建议**按年份分批**跑（GitHub Actions 单 job 上限 6 小时）。
