"""
除權息還原（含減資）
股票除權息或減資那天，股價會因為配股配息、減資而跳動，這不是真的漲跌。
不還原的話，季線、盤整區間、一年位置都會算錯。

調整比例＝除權息（或減資恢復買賣）參考價 ÷ 前一天收盤價
某一天的還原價＝原始價 × 那天之後所有事件的調整比例相乘

資料來源：
- 證交所 除權除息計算結果表 TWT49U（上市，可一次抓一年）
- 證交所 減資恢復買賣參考價 TWTAUU（上市）
- 櫃買中心 除權除息計算結果 exDailyQ（上櫃，半年一抓）
歷年資料抓過就存在 cache/exr.json.gz，之後每次只補最近 2 個月。
"""
import gzip, json, os, re, time, datetime as dt, urllib.request

UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
EXR = "cache/exr.json.gz"
TW = dt.timezone(dt.timedelta(hours=8))


def getj(url):
    for i in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print("  retry", url[-60:], e, flush=True)
            time.sleep(15)
    return None


def n(v):
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s not in ("", "--", "-") else None
    except Exception:
        return None


def roc(s):
    """'114年06月02日'、'114/06/02' → '2025-06-02'"""
    m = re.findall(r"\d+", str(s))
    if len(m) < 3:
        return None
    y = int(m[0])
    y = y + 1911 if y < 1000 else y
    return f"{y}-{int(m[1]):02d}-{int(m[2]):02d}"


def fetch_range(a, b):
    """抓 a～b（date）期間的所有事件 → [(代號, 日期, 比例)]"""
    ev = []
    j = getj(f"https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate={a:%Y%m%d}&endDate={b:%Y%m%d}&response=json")
    time.sleep(3)
    for r in (j or {}).get("data", []) if (j or {}).get("stat") == "OK" else []:
        d, c, p0, p1 = roc(r[0]), str(r[1]).strip(), n(r[3]), n(r[4])
        if d and p0 and p1 and re.fullmatch(r"\d{4}", c):
            ev.append((c, d, p1 / p0))
    j = getj(f"https://www.twse.com.tw/rwd/zh/reducation/TWTAUU?startDate={a:%Y%m%d}&endDate={b:%Y%m%d}&response=json")
    time.sleep(3)
    for r in (j or {}).get("data", []) if (j or {}).get("stat") == "OK" else []:
        d, c, p0, p1 = roc(r[0]), str(r[1]).strip(), n(r[3]), n(r[4])
        if d and p0 and p1 and re.fullmatch(r"\d{4}", c):
            ev.append((c, d, p1 / p0))
    # 櫃買中心一次最多抓約半年
    s = a
    while s <= b:
        e = min(b, s + dt.timedelta(days=182))
        j = getj(f"https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate={s:%Y/%m/%d}&endDate={e:%Y/%m/%d}&response=json")
        time.sleep(3)
        t = ((j or {}).get("tables") or [{}])[0]
        for r in t.get("data", []):
            d, c, p0, p1 = roc(r[0]), str(r[1]).strip(), n(r[3]), n(r[4])
            if d and p0 and p1 and re.fullmatch(r"\d{4}", c):
                ev.append((c, d, p1 / p0))
        s = e + dt.timedelta(days=1)
    return ev


def update(first_date):
    """回傳 {代號: [[日期, 比例], ...]}（日期由舊到新）"""
    db = {"done": [], "ev": {}}
    if os.path.exists(EXR):
        try:
            with gzip.open(EXR, "rt", encoding="utf-8") as f:
                db = json.load(f)
        except Exception:
            pass
    today = dt.datetime.now(TW).date()
    y0 = int(first_date[:4])
    ranges = []
    for y in range(y0, today.year):           # 過去完整年份只抓一次
        if str(y) not in db["done"]:
            ranges.append((str(y), dt.date(y, 1, 1), dt.date(y, 12, 31)))
    ranges.append(("recent", today - dt.timedelta(days=60), today))  # 最近 2 個月每次都更新
    seen = {(c, d) for c, l in db["ev"].items() for d, _ in l}
    t0 = time.time()
    for key, a, b in ranges:
        if time.time() - t0 > 300:
            print("  除權息資料時間到，剩下的下次再補", flush=True)
            break
        got = fetch_range(a, b)
        for c, d, r in got:
            if (c, d) not in seen and 0.05 < r < 20:
                db["ev"].setdefault(c, []).append([d, round(r, 6)])
                seen.add((c, d))
        if key != "recent" and got:
            db["done"].append(key)
        print(f"  除權息 {key}：{len(got)} 筆", flush=True)
    for c in db["ev"]:
        db["ev"][c].sort()
    os.makedirs("cache", exist_ok=True)
    with gzip.open(EXR, "wt", encoding="utf-8") as f:
        json.dump(db, f, separators=(",", ":"))
    print(f"除權息／減資事件：{sum(len(v) for v in db['ev'].values())} 筆，{len(db['ev'])} 檔", flush=True)
    return db["ev"]


def factors(dates, events):
    """回傳每個日期的還原係數（該日之後所有事件比例相乘）"""
    f = [1.0] * len(dates)
    if not events:
        return f
    ev = sorted(events)
    k, mult = len(ev) - 1, 1.0
    for i in range(len(dates) - 1, -1, -1):
        while k >= 0 and ev[k][0] > dates[i]:
            mult *= ev[k][1]
            k -= 1
        f[i] = mult
    return f
