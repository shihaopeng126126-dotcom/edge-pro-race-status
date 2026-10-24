/**
 * Cloudflare Workers 版 PCS 采集器（轻量 · 只下载不解析）
 * ---------------------------------------------------------------
 * 思路：境外只负责「把 HTML 原样下载下来」，解析与入库放在境内做。
 *   好处 1：Workers 免费额度的 CPU 限制完全够用（几乎不消耗 CPU）
 *   好处 2：解析逻辑改了不用重新部署境外节点
 *   好处 3：原始 HTML 留存，可回溯、可重解析
 *
 * ⚠️ 重要：*.workers.dev 在国内不可达（实测连接超时）。
 *    所以本 worker 只能是【主动 POST 到境内接口】，
 *    千万不要设计成「境内去请求 worker URL」，那样必失败。
 *
 * 部署：
 *   npx wrangler deploy
 *   npx wrangler secret put INGEST_URL
 *   npx wrangler secret put INGEST_SECRET
 *
 * 定时：wrangler.toml 里配置 [triggers] crons = ["0 2 * * *"]
 */

export default {
  async scheduled(event, env, ctx) {
    const targets = (env.TARGETS || 'race/tour-de-france/2026,rider/tadej-pogacar')
      .split(',')
      .map(s => s.trim())
      .filter(Boolean);

    const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
             + '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

    const items = [];
    for (const path of targets) {
      const url = 'https://www.procyclingstats.com/' + path.replace(/^\//, '');
      try {
        const res = await fetch(url, {
          headers: { 'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9' },
          cf: { cacheTtl: 300 },
        });
        if (res.status === 403) {
          console.error(`403 blocked: ${path} —— 出口 IP 仍在被拦，检查节点位置`);
          continue; // 不要重试轰炸
        }
        if (!res.ok) { console.error(`${res.status} ${path}`); continue; }
        items.push({
          path,
          url,
          status: res.status,
          fetched_at: new Date().toISOString(),
          html: await res.text(),
        });
      } catch (e) {
        console.error(`fetch failed ${path}: ${e}`);
      }
      await new Promise(r => setTimeout(r, 1200)); // 限速：≤1 req/s，对公益站保持克制
    }

    if (!items.length) return;
    await push(env, items);
  },
};

async function push(env, items) {
  const body = JSON.stringify({ items });
  const ts = String(Math.floor(Date.now() / 1000));

  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(env.INGEST_SECRET),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  );
  const sigBuf = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(ts + body));
  const sig = [...new Uint8Array(sigBuf)].map(b => b.toString(16).padStart(2, '0')).join('');

  const res = await fetch(env.INGEST_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'X-Timestamp': ts,
      'X-Signature': sig,
    },
    body,
  });
  console.log(`pushed ${items.length} items -> ${res.status}`);
}
