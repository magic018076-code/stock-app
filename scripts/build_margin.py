"""
融資融券（每天全市場每一檔的融資餘額、融券餘額、融資限額）
- 證交所 MI_MARGN（上市，selectType=ALL，一天一次請求）
- 櫃買中心 margin/balance（上櫃，一天一次請求）
兩邊都可以回溯到 2008 年。存在 cache/margin.json.gz：{日期: {代號: [融資餘額, 融券餘額, 融資限額]}}（張）
新的日期先抓；舊資料分很多輪慢慢補（每輪有時間上限，還沒補完會寫 cache/more.flag 讓下一輪接著跑）。
"""
import datetime as dt, os, re, time
from build_fund import getj, n, load, save, TW

MARGIN = "cache/margin.json.gz"
FROM = "2008-01-02"
SLEEP = 2.5


def fetch_day(d):
    """回傳 {代號: [融資餘額, 融券餘額, 融資限額]}；兩邊都沒有資料回傳 {}"""
    out = {}
    j = getj(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={d.replace('-', '')}&selectType=ALL&response=json", 2, 30)
    time.sleep(SLEEP)
    if j is None:
        raise RuntimeError("證交所沒有回應")
    tw_ok = j.get("stat") == "OK"
    for t in j.get("tables") or []:
        f = t.get("fields") or []
        if len(f) < 13 or f[0] != "代號":
            continue
        for r in t.get("data") or []:
            c = str(r[0]).strip()
            if re.fullmatch(r"\d{4}", c) and len(r) >= 13:
                out[c] = [round(n(r[6]) or 0), round(n(r[12]) or 0), round(n(r[7]) or 0)]
    j = getj(f"https://www.tpex.org.tw/www/zh-tw/margin/balance?date={d.replace('-', '/')}&response=json", 2, 30)
    time.sleep(SLEEP)
    if j is None and tw_ok:
        raise RuntimeError("櫃買中心沒有回應")   # 只抓到上市的不要存，下次重抓
    for t in (j or {}).get("tables") or []:
        for r in t.get("data") or []:
            c = str(r[0]).strip()
            if re.fullmatch(r"\d{4}", c) and len(r) >= 15:
                out[c] = [round(n(r[6]) or 0), round(n(r[14]) or 0), round(n(r[9]) or 0)]
    return out


def update(dates):
    db = load(MARGIN, {})
    want = [d for d in dates if d >= FROM]
    today = dt.datetime.now(TW).date()
    # 排程的那兩輪要先把今天的資料發布出去，只補 20 分鐘；接力補資料的輪次可以跑 2.5 小時
    dflt = "9000" if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" else "1200"
    budget = int(os.environ.get("MARGIN_SECS") or dflt)
    t0, k, left = time.time(), 0, 0
    for d in want[::-1]:
        if d in db:
            continue
        if time.time() - t0 > budget:
            left += 1
            continue
        try:
            got = fetch_day(d)
            if got:
                db[d] = got
                print(f"  融資券 {d}：{len(got)} 檔", flush=True)
            elif (today - dt.date.fromisoformat(d)).days >= 5:
                db[d] = {}
            k += 1
            if k % 50 == 0:
                save(MARGIN, db)
        except Exception as e:
            print("  融資券失敗", d, e, flush=True)
    if left:
        print(f"  融資券資料時間到，還有 {left} 天下次再補", flush=True)
        with open("cache/more.flag", "a") as f:
            f.write("margin")
    save(MARGIN, db)
    days = [d for d in sorted(db) if db[d]]
    print(f"融資券資料：{len(days)} 天", flush=True)
    return {d: db[d] for d in days}


def summary(db):
    """每檔最新的融資券狀況（給 fund.json），和全市場融資餘額（近 250 天）"""
    days = sorted(db)
    if not days:
        return {}, []
    last = db[days[-1]]
    ago = db[days[-21]] if len(days) > 21 else {}
    s = {}
    for c, v in last.items():
        mb, sb, lim = v
        o = {"mb": mb, "sb": sb}
        if lim > 0:
            o["mu"] = round(mb / lim * 100, 1)                  # 融資使用率
        if mb >= 50:
            o["ssr"] = round(sb / mb * 100, 1)                  # 券資比
        p = ago.get(c)
        if p and p[0] >= 50:
            o["mb20"] = round((mb / p[0] - 1) * 100, 1)         # 融資 20 日增減 %
        s[c] = o
    # 全市場融資餘額：只用兩個市場都有資料的日子（檔數夠多）
    big = max(len(db[d]) for d in days[-30:])
    mk = [[int(d.replace("-", "")), sum(v[0] for v in db[d].values())] for d in days[-250:] if len(db[d]) >= big * 0.8]
    return s, mk
