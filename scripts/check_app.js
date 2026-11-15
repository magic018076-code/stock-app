/*
  App 功能總檢查（在 GitHub Actions 上用真實網路跑）
  用無頭 Chrome 打開 www/，逐一點過每個功能，記錄：
  - 每個步驟是否正常（有沒有出現資料）
  - 程式錯誤（JavaScript error）
  - 每個資料來源的連線結果
  - 每一頁的截圖
  輸出到 check-out/：report.md、report.json、*.png
*/
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..", "www");
const OUT = path.join(__dirname, "..", "check-out");
fs.mkdirSync(OUT, { recursive: true });

const MIME = { ".html": "text/html; charset=utf-8", ".json": "application/json", ".js": "text/javascript" };
const server = http.createServer((req, res) => {
  const f = path.join(ROOT, decodeURIComponent(req.url.split("?")[0]).replace(/^\/+/, "") || "index.html");
  if (!f.startsWith(ROOT) || !fs.existsSync(f)) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
  fs.createReadStream(f).pipe(res);
});

const steps = [], jsErr = [], net = {};
let shotN = 0;
function rec(name, ok, detail = "") { steps.push({ name, ok, detail }); console.log(ok ? "✅" : "❌", name, detail); }

(async () => {
  await new Promise(r => server.listen(8799, r));
  const browser = await chromium.launch({ args: ["--disable-web-security", "--disable-site-isolation-trials"] });
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, locale: "zh-TW", timezoneId: "Asia/Taipei" });
  const p = await ctx.newPage();
  p.on("pageerror", e => jsErr.push(e.message));
  p.on("console", m => { if (m.type() === "error" && !/Failed to load resource|net::ERR/.test(m.text())) jsErr.push("console: " + m.text().slice(0, 200)); });
  p.on("requestfinished", async rq => {
    const u = new URL(rq.url()); if (u.hostname === "localhost") return;
    const r = await rq.response(); const k = u.hostname + u.pathname.replace(/\d{4,}/g, "N");
    (net[k] = net[k] || { ok: 0, bad: 0, last: "" }); if (r && r.ok()) net[k].ok++; else { net[k].bad++; net[k].last = "HTTP " + (r ? r.status() : "?"); }
  });
  p.on("requestfailed", rq => {
    const u = new URL(rq.url()); if (u.hostname === "localhost") return;
    const k = u.hostname + u.pathname.replace(/\d{4,}/g, "N");
    (net[k] = net[k] || { ok: 0, bad: 0, last: "" }); net[k].bad++; net[k].last = rq.failure() ? rq.failure().errorText : "failed";
  });

  const shot = async name => { const f = `${String(++shotN).padStart(2, "0")}-${name}.png`; await p.screenshot({ path: path.join(OUT, f) }); return f; };
  const wait = ms => p.waitForTimeout(ms);
  const txt = sel => p.$eval(sel, e => e.innerText).catch(() => "");
  const count = sel => p.$$eval(sel, e => e.length).catch(() => 0);
  const back = async () => { await p.click("#ovBack").catch(() => {}); await wait(400); };
  const home = async () => { await p.click("nav button[data-t=home]"); await wait(500); };
  const step = async (name, fn) => { try { await fn(); } catch (e) { rec(name, false, "例外：" + e.message.split("\n")[0]); await shot("err-" + name).catch(() => {}); } };

  // 1. 介面自動更新載入器
  await step("介面自動更新（OTA）", async () => {
    await p.goto("http://localhost:8799/index.html"); await wait(9000);
    const v = await p.evaluate(() => [window.__OTA_V, window.__otaReady, window.__appOK]);
    const remote = JSON.parse(fs.readFileSync(path.join(ROOT, "ota.json"))).v;
    rec("介面自動更新（OTA）", v[2] === true, `App 啟動=${v[2]}，內建介面 v${remote}，GitHub 上的版本檢查=${v[1] ? "有新版 v" + v[1] : "已是最新"}`);
  });

  await p.goto("http://localhost:8799/app.html"); await wait(12000);

  await step("首頁", async () => {
    const m = await p.$$eval(".mstrip b", e => e.map(x => x.innerText));
    rec("首頁：加權指／櫃買指／台指近", m.filter(x => x !== "—").length === 3, m.join("、"));
    const tiles = await count(".grid [data-tile]"); rec("首頁：功能圖示", tiles === 15, tiles + " 個");
    rec("首頁：低檔選股紅色數字", true, (await txt(".grid [data-tile=pick] em")) || "（今天沒有）");
    await shot("首頁");
  });

  await step("指數行情", async () => {
    await p.click(".grid [data-tile=index]"); await wait(2500);
    const rows = await p.$$eval(".qr", e => e.map(x => x.innerText.replace(/\s+/g, " ")));
    const empty = rows.filter(r => /—\s*—/.test(r));
    rec("指數行情：指數與國際", rows.length > 10 && empty.length <= 1, `${rows.length} 列，沒資料 ${empty.length} 列 ${empty.join("｜").slice(0, 120)}`);
    const kv = await txt(".kv"); rec("指數行情：市場概況（家數、漲跌、成交值）", !/—/.test(kv.split("\n").slice(0, 8).join("")), kv.replace(/\s+/g, " ").slice(0, 120));
    rec("指數行情：三大法人（市場）", /外資/.test(await txt("#main")) && (await count(".qr")) > 0, "");
    await shot("指數行情"); await home();
  });

  await step("自選報價", async () => {
    await p.click("nav button[data-t=watch]"); await wait(4000);
    const rows = await p.$$eval(".qr", e => e.map(x => x.innerText.replace(/\s+/g, " ")));
    rec("自選報價：報價", rows.length >= 4 && rows.every(r => !/—\s*—\s*—/.test(r)), rows.slice(0, 6).join("｜"));
    rec("自選報價：資料時間", true, (await txt(".foot")).split("\n")[0]);
    await shot("自選報價");
    for (const [k, n] of [["p:vol", "成交量排行"], ["p:up", "漲幅排行"], ["p:down", "跌幅排行"], ["p:otcvol", "上櫃成交量排行"], ["p:low", "低檔盤整清單"]]) {
      await p.click("#tbT"); await wait(300); await p.click(`#sheet [data-sel="${k}"]`); await wait(2500);
      const c = await count(".qr[data-code]"); rec("自選報價：" + n, c >= 10, c + " 檔");
    }
    await shot("排行");
    await p.click("#tbT"); await wait(300); await p.click('#sheet [data-sel="g:0"]'); await wait(800);
    await p.click("#tbEdit"); await wait(500); rec("自選報價：編輯清單", (await count("#ovBody .ed")) > 0, (await count("#ovBody .ed")) + " 檔"); await shot("編輯清單"); await back();
  });

  await step("加入自選（搜尋）", async () => {
    await p.click("nav button[data-t=add]"); await wait(600);
    await p.fill("#sq", "環球晶"); await wait(800);
    const r = await count("#ovBody [data-add], #ovBody [data-code]"); rec("加入自選：搜尋「環球晶」", r > 0, r + " 筆結果");
    await shot("搜尋"); await back();
  });

  for (const [c, n] of [["2330", "台積電（上市）"], ["6488", "環球晶（上櫃）"]]) {
    await step("個股頁 " + n, async () => {
      await p.evaluate(c => openStock(c), c); await wait(9000);
      rec(`個股 ${n}：報價`, /\d/.test(await txt(".big .px")), (await txt(".big")).replace(/\s+/g, " "));
      rec(`個股 ${n}：日K線`, (await count("#kc rect")) > 20, (await count("#kc rect")) + " 個圖形");
      rec(`個股 ${n}：三大法人`, /外資/.test(await txt("#ovBody")) && !/籌碼\s*讀取中/.test(await txt("#ovBody")), "");
      rec(`個股 ${n}：融資融券`, /融資餘額/.test(await txt("#ovBody")), "");
      rec(`個股 ${n}：五檔`, /五檔報價/.test(await txt("#ovBody")), /五檔報價/.test(await txt("#ovBody")) ? "" : "（休市時可能沒有）");
      rec(`個股 ${n}：公司資料`, /公司資料/.test(await txt("#ovBody")), "");
      await shot("個股-" + c);
      await p.click("#ktab [data-k=w]"); await wait(6000); rec(`個股 ${n}：週K線`, (await count("#kc rect")) > 20, (await count("#kc rect")) + " 個圖形");
      await p.click("#ktab [data-k=m]"); await wait(12000); rec(`個股 ${n}：月K線`, (await count("#kc rect")) > 10, (await count("#kc rect")) + " 個圖形");
      await shot("個股月K-" + c);
      await p.click("#ktab [data-k=d]"); await wait(300); await back();
    });
  }

  await step("庫存與提醒", async () => {
    await p.evaluate(() => openStock("2330")); await wait(3000);
    await p.fill("#hQty", "1000"); await p.fill("#hCost", "1000"); await p.click("#hSave"); await wait(400);
    await p.fill("#aPx", "99999"); await p.click("#aUp"); await wait(400); await back(); await home();
    await p.click(".grid [data-tile=holds]"); await wait(1500);
    rec("庫存損益", (await count(".qr[data-code]")) === 1 && /\d/.test(await txt(".hsum")), (await txt(".hsum")).replace(/\s+/g, " "));
    await shot("庫存損益"); await home();
    await p.click(".grid [data-tile=alerts]"); await wait(600);
    rec("價格提醒", /99999/.test(await txt("#ovBody")), ""); await shot("價格提醒"); await back();
  });

  await step("低檔選股", async () => {
    await p.click("nav button[data-t=pick]"); await wait(3000);
    const seg = await txt(".seg"); rec("低檔選股：資料", (await count(".lc")) > 0 || /起漲訊號\s*0/.test(seg), seg.replace(/\s+/g, " "));
    rec("低檔選股：資料日期", true, await txt(".sec span:last-child"));
    await shot("低檔選股");
    for (const v of ["all", "bo"]) { await p.click(`[data-lbv=${v}]`); await wait(400); }
    rec("低檔選股：全部低檔", true, (await txt(".seg")).replace(/\s+/g, " "));
    for (const [k, val, n] of [["per", "1200", "盤整 5 年"], ["per", "2400", "盤整 10 年"], ["amp", "30", "振幅 30%"], ["mkt", "O", "上櫃"]]) {
      await p.click(`[data-lbm=${k}]`); await wait(300); await p.click(`#sheet [data-sel="${val}"]`); await wait(400);
      rec("低檔選股：" + n, !(await count(".pageerror")), `${await count(".lc")} 檔`);
    }
    await shot("低檔選股-條件");
    await p.click("[data-lbreset]"); await wait(400); await p.click("[data-lbv=sig]"); await wait(400);
    if (await count(".lc")) { await p.click(".lc"); await wait(6000); rec("低檔選股：個股低檔分析", /低檔分析/.test(await txt("#ovBody")), ""); await shot("低檔分析"); await back(); }
  });

  await step("國際金融", async () => {
    for (const [k, n] of [["us", "美股指數"], ["usstk", "美股個股"], ["day", "台指期日盤"], ["night", "台指期夜盤"]]) {
      await p.evaluate(k => { iSel = k; go("intl") }, k); await wait(4000);
      const rows = await p.$$eval(".qr", e => e.map(x => x.innerText.replace(/\s+/g, " ")));
      rec("國際金融：" + n, rows.length > 0 && !/讀取中/.test(await txt("#main")), `${rows.length} 列 ${rows.slice(0, 3).join("｜")}`);
      await shot("國際-" + k);
    }
    await home();
  });

  for (const [k, n, sel, min] of [["ind", "類股報價", "#ovBody [data-indsel]", 10], ["news", "財經新聞", "#ovBody .news", 5], ["mops", "重大訊息", "#ovBody .news", 5], ["newlist", "新上市櫃", "#ovBody .ed", 10], ["otchist", "上櫃家數", "#ovBody .li, #ovBody .qr, #ovBody div", 1], ["settings", "設定", "#ovBody .li", 3]]) {
    await step(n, async () => {
      await p.click(`.grid [data-tile=${k}]`); await wait(3500);
      const c = await count(sel); rec(n, c >= min, c + " 筆");
      if (k === "ind" && c) { await p.click("#ovBody [data-indsel]"); await wait(4000); rec("類股報價：點進產業", (await count("#ovBody .qr")) > 0, (await count("#ovBody .qr")) + " 檔"); await shot("類股內容"); await back(); }
      await shot(n); await back(); await home();
    });
  }

  await step("檢查更新（APK）", async () => {
    await p.click(".grid [data-tile=upd]"); await wait(4000);
    const v = await p.evaluate(() => remoteVer); rec("檢查更新（APK）", !!(v && v.versionCode), JSON.stringify(v));
  });

  await step("資料狀態", async () => {
    await p.click(".grid [data-tile=status]"); await wait(600);
    const rows = await p.$$eval("#ovBody .li", e => e.map(x => x.innerText.replace(/\s+/g, " ")));
    for (const r of rows) rec("資料來源：" + r.replace(/[✅⚠️].*$/u, "").trim(), /✅/.test(r), r.replace(/^.*?([✅⚠️])/u, "$1"));
    await shot("資料狀態"); await back();
  });

  await browser.close(); server.close();

  // 報告
  const bad = steps.filter(s => !s.ok);
  let md = `# App 功能檢查報告\n\n檢查時間：${new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 16).replace("T", " ")}（台灣時間）\n\n`;
  md += `**結果：${steps.length - bad.length} 項正常、${bad.length} 項有問題、程式錯誤 ${jsErr.length} 個**\n\n## 各項功能\n\n| 狀態 | 項目 | 說明 |\n|---|---|---|\n`;
  md += steps.map(s => `| ${s.ok ? "✅" : "❌"} | ${s.name} | ${String(s.detail).replace(/\|/g, "／").slice(0, 160)} |`).join("\n");
  md += `\n\n## 資料來源連線\n\n| 網址 | 成功 | 失敗 | 最後錯誤 |\n|---|---|---|---|\n` + Object.entries(net).sort().map(([k, v]) => `| ${k} | ${v.ok} | ${v.bad} | ${v.last} |`).join("\n");
  md += `\n\n## 程式錯誤\n\n` + (jsErr.length ? jsErr.map(e => "- " + e).join("\n") : "沒有") + "\n";
  fs.writeFileSync(path.join(OUT, "report.md"), md);
  fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify({ steps, net, jsErr }, null, 1));
  console.log(md);
})().catch(e => { console.error(e); process.exit(1); });
