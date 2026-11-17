"""
基本面與籌碼資料（每日收盤後由 build_lowbase.py 呼叫）
1. 本益比、殖利率、淨值比：證交所 BWIBBU_ALL、櫃買中心本益比分析
2. 每月營收：最新一個月用 OpenAPI，過去月份從公開資訊觀測站備用站 CSV 補齊，保留 10 年
3. 三大法人：每天記錄外資、投信買賣超，保留約 250 個交易日，算近 5／20／60 日合計與連買賣天數

輸出 out/fund.json：{"date":..., "s": {代號: {...}}}
另外把每檔的營收歷史交給 K 線檔（rv 欄位）
任何一部分失敗都不會讓整個計算中斷。
"""
import gzip, json, os, re, time, datetime as dt, urllib.request

UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
TW = dt.timezone(dt.timedelta(hours=8))
SLEEP = 2.2
INSTI = "cache/insti.json.gz"
REV = "cache/rev.json.gz"


def raw(url, tries=3, timeout=60):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            print("  retry", i + 1, url[-70:], e, flush=True)
            time.sleep(10 * (i + 1))
    return None


def getj(url, tries=3, timeout=60):
    b = raw(url, tries, timeout)
    if b is None:
        return None
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:
        return None


def n(v):
    try:
        s = str(v).replace(",", "").replace("+", "").strip()
        if s in ("", "--", "-", "X", "---", "N/A"):
            return None
        return float(s)
    except Exception:
        return None


def pick(r, *subs):
    """依欄位名稱的片段找值（OpenAPI 欄位名稱中英文都有，不同市場不一樣）"""
    for s in subs:
        for k, v in r.items():
            if all(x in k for x in s.split("&")):
                return v
    return None


def load(p, d):
    if os.path.exists(p):
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return d


def save(p, v):
    os.makedirs("cache", exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, separators=(",", ":"))


# ---------- 1. 本益比、殖利率、淨值比 ----------
def fetch_val():
    out = {}
    for r in getj("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL") or []:
        c = str(pick(r, "Code", "證券代號") or "").strip()
        if c:
            out[c] = [n(pick(r, "PEratio", "本益比")), n(pick(r, "DividendYield", "殖利率")), n(pick(r, "PBratio", "股價淨值比"))]
    for r in getj("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis") or []:
        c = str(pick(r, "SecuritiesCompanyCode", "股票代號", "代號") or "").strip()
        if c:
            out[c] = [n(pick(r, "PriceEarningRatio", "本益比")), n(pick(r, "YieldRatio", "殖利率")), n(pick(r, "PriceBookRatio", "淨值比"))]
    print(f"本益比／殖利率／淨值比：{len(out)} 檔", flush=True)
    return out


# ---------- 2. 每月營收 ----------
def ym_list(k):
    t = dt.datetime.now(TW).date().replace(day=1)
    out = []
    for _ in range(k):
        t = (t - dt.timedelta(days=1)).replace(day=1)
        out.append(f"{t.year}{t.month:02d}")
    return out  # 由近到遠：上個月、上上個月…


def fetch_rev_latest(rev):
    got = 0
    for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"):
        for r in getj(url) or []:
            c = str(pick(r, "公司代號", "CompanyCode", "SecuritiesCompanyCode") or "").strip()
            ym = str(pick(r, "資料年月", "DataYearMonth", "YearMonth") or "").strip()
            if not c or not re.fullmatch(r"\d{4,5}", ym):
                continue
            ym = f"{int(ym[:-2]) + 1911}{ym[-2:]}"
            v = n(pick(r, "當月營收&營業收入", "MonthlyRevenue", "當月營收"))
            if v is None:
                continue
            rev.setdefault(ym, {})[c] = [v, n(pick(r, "上月比較", "LastMonthComparison")), n(pick(r, "去年同月增減", "LastYearMonthComparison"))]
            # 順便記下「上個月營收」，歷史營收網站連不上時也至少有兩個月
            pv = n(pick(r, "上月營收&營業收入", "LastMonthRevenue"))
            pym = f"{int(ym[:4]) - (1 if ym[4:] == '01' else 0)}{(int(ym[4:]) - 2) % 12 + 1:02d}"
            if pv is not None and c not in rev.get(pym, {}):
                rev.setdefault(pym, {})[c] = [pv, None, None]
            got += 1
    print(f"最新月營收：{got} 筆", flush=True)


DEAD = set()  # 這次執行中連不上的網站，不再嘗試
REV_MONTHS = 120  # 月營收保留 10 年


def fetch_rev_month(ym):
    """從公開資訊觀測站（備用站 mopsov）下載整月營收 CSV：上市 sii、上櫃 otc"""
    import csv, io
    y, m = int(ym[:4]) - 1911, int(ym[4:])
    out = {}
    for mk in ("sii", "otc"):
        if "mopsov" in DEAD:
            break
        url = f"https://mopsov.twse.com.tw/server-java/FileDownLoad?step=9&functionName=show_file2&filePath=%2Ft21%2F{mk}%2F&fileName=t21sc03_{y}_{m}.csv"
        b = raw(url, tries=2, timeout=40)
        time.sleep(5)  # 觀測站有流量限制，放慢一點
        if not b:
            print("   mopsov 暫時拒絕，等 60 秒再試", flush=True)
            time.sleep(60)
            b = raw(url, tries=1, timeout=40)
        if not b:
            DEAD.add("mopsov")
            print("   mopsov 連不上，這次不再嘗試", flush=True)
            break
        txt = None
        for enc in ("utf-8-sig", "cp950", "big5"):
            try:
                txt = b.decode(enc)
                break
            except Exception:
                pass
        if not txt:
            continue
        rows = list(csv.reader(io.StringIO(txt)))
        if not rows:
            continue
        hd = [h.strip() for h in rows[0]]
        ix = lambda name, d: next((i for i, h in enumerate(hd) if name in h), d)
        ic, iv, im, iy = ix("公司代號", 2), ix("當月營收", 5), ix("上月比較", 8), ix("去年同月增減", 9)
        for r in rows[1:]:
            if len(r) <= max(ic, iv, im, iy):
                continue
            c = r[ic].strip()
            v = n(r[iv])
            if re.fullmatch(r"\d{4}", c) and v is not None:
                out[c] = [v, n(r[im]), n(r[iy])]
    print(f"  補 {ym} 月營收：{len(out)} 筆", flush=True)
    return out


def update_rev():
    rev = load(REV, {})
    try:
        fetch_rev_latest(rev)
    except Exception as e:
        print("最新月營收失敗", e, flush=True)
    want = ym_list(REV_MONTHS)
    t0 = time.time()
    for ym in want:  # 由近到遠補
        if time.time() - t0 > 600 or DEAD:
            print("  補月營收時間到或網站連不上，剩下的下次再補", flush=True)
            break
        if len(rev.get(ym, {})) < 800:  # 沒有或太少就補（每月上市櫃約 1,800 家）
            try:
                got = fetch_rev_month(ym)
                if got:
                    rev[ym] = {**got, **rev.get(ym, {})}  # 已有的（OpenAPI）優先
                    save(REV, rev)
            except Exception as e:
                print("  補月營收失敗", ym, e, flush=True)
    rev = {k: v for k, v in rev.items() if k >= want[-1]}
    save(REV, rev)
    print(f"月營收：{sorted(rev)[:1]}～{sorted(rev)[-1:]}，共 {len(rev)} 個月", flush=True)
    return rev


# ---------- 3. 三大法人 ----------
INSTI_FROM = "2012-05-02"   # 證交所三大法人個股日報最早的日期


def fetch_insti_day(d):
    """回傳 {代號: [外資, 投信, 自營]}（張）；資料還沒公布回傳 None。
    證交所欄位在 2014 年底、2017 年底改過，依欄位名稱找；櫃買中心依欄位數判斷新舊格式。"""
    out = {}
    j = getj(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d.replace('-', '')}&selectType=ALLBUT0999&response=json", 2, 30)
    time.sleep(SLEEP)
    if j is None:
        raise RuntimeError("證交所沒有回應")
    if j.get("stat") == "OK":
        f = [str(x).replace(" ", "") for x in (j.get("fields") or [])]
        fi = [i for i, x in enumerate(f) if "買賣超" in x and ("外資" in x or "外陸資" in x)]      # 外資（新格式有兩欄：不含自營商＋外資自營商）
        ti = next((i for i, x in enumerate(f) if x.startswith("投信買賣超")), None)
        di = next((i for i, x in enumerate(f) if x == "自營商買賣超股數"), None)
        if fi and ti is not None:
            for r in j.get("data", []):
                c = str(r[0]).strip()
                if re.fullmatch(r"\d{4}", c) and len(r) > max(fi + [ti]):
                    out[c] = [round(sum(n(r[i]) or 0 for i in fi) / 1000), round((n(r[ti]) or 0) / 1000), round((n(r[di]) or 0) / 1000) if di is not None and len(r) > di else 0]
    j = getj(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={d.replace('-', '/')}&response=json", 2, 30)
    time.sleep(SLEEP)
    t = (j or {}).get("tables") or [{}]
    for r in t[0].get("data", []) if t else []:
        c = str(r[0]).strip()
        if not re.fullmatch(r"\d{4}", c):
            continue
        if len(r) >= 23:      # 2018 年起的新格式
            out[c] = [round((n(r[10]) or 0) / 1000), round((n(r[13]) or 0) / 1000), round((n(r[22]) or 0) / 1000)]
        elif len(r) >= 9:     # 舊格式
            out[c] = [round((n(r[4]) or 0) / 1000), round((n(r[7]) or 0) / 1000), round((n(r[8]) or 0) / 1000)]
    return out


def update_insti(dates):
    ins = load(INSTI, {})
    want = [d for d in dates if d >= INSTI_FROM]   # 從證交所有資料的第一天起全部保留
    today = dt.datetime.now(TW).date()
    t0, k, left = time.time(), 0, 0
    # 股價還在補的時候法人最多跑 45 分鐘；股價補完後每輪可以跑 3 小時，加快補齊
    budget = int(os.environ.get("INSTI_SECS") or ("2700" if os.path.exists("cache/more.flag") else "10800"))
    for d in want[::-1]:  # 新的日期先抓
        if d in ins:
            continue
        if time.time() - t0 > budget:
            left += 1
            continue
        try:
            got = fetch_insti_day(d)
            if got:
                ins[d] = got
                print(f"  法人 {d}：{len(got)} 檔", flush=True)
            elif (today - dt.date.fromisoformat(d)).days >= 5:
                ins[d] = {}        # 很久以前的日期沒有資料：記下來，不再重抓
            k += 1
            if k % 50 == 0:
                save(INSTI, ins)
        except Exception as e:
            print("  法人失敗", d, e, flush=True)
    if left:
        print(f"  法人資料時間到，還有 {left} 天下次再補", flush=True)
        open("cache/more.flag", "w").write("insti")
    ins = {k_: v for k_, v in ins.items() if k_ >= INSTI_FROM}
    save(INSTI, ins)
    return {k_: v for k_, v in ins.items() if v}    # 只回傳有資料的日子


def streak(vals):
    """最近連續買超（正）或賣超（負）天數"""
    s = 0
    for v in reversed(vals):
        if v > 0 and s >= 0:
            s += 1
        elif v < 0 and s <= 0:
            s -= 1
        else:
            break
    return s


def run(dates):
    """回傳 (fund, revHist, 法人每日)：fund 給 App 的 fund.json；revHist 給 K 線檔"""
    val, rev, ins = {}, {}, {}
    for name, fn in (("本益比", fetch_val), ("營收", update_rev), ("法人", lambda: update_insti(dates))):
        try:
            r = fn()
            if name == "本益比":
                val = r
            elif name == "營收":
                rev = r
            else:
                ins = r
        except Exception as e:
            print(name, "失敗", e, flush=True)
    s = {}
    for c, v in val.items():
        s.setdefault(c, {})["pe"], s[c]["dy"], s[c]["pb"] = v
    yms = sorted(rev)
    rv_hist = {}
    for ym in yms:
        for c, v in rev[ym].items():
            r1 = lambda x: round(x, 1) if isinstance(x, (int, float)) else None
            rv_hist.setdefault(c, []).append([int(ym), round(v[0]), r1(v[1]), r1(v[2])])
    for c, h in rv_hist.items():
        last = h[-1]
        s.setdefault(c, {}).update({"rv": last[1], "rvm": last[2], "rvy": last[3], "rym": last[0]})
    days = sorted(ins)
    codes = set().union(*[set(ins[d]) for d in days]) if days else set()
    for c in codes:
        f = [ins[d].get(c, [0, 0, 0])[0] for d in days]
        t = [ins[d].get(c, [0, 0, 0])[1] for d in days]
        s.setdefault(c, {}).update({"f5": sum(f[-5:]), "f20": sum(f[-20:]), "f60": sum(f[-60:]), "fs": streak(f),
                                    "t5": sum(t[-5:]), "t20": sum(t[-20:]), "t60": sum(t[-60:]), "ts": streak(t)})
    # 每檔最近 120 天的外資、投信買賣超，給 K 線檔（個股頁畫圖）
    fi = {}
    for d in days[-120:]:
        dn = int(d.replace("-", ""))
        for c, v in ins[d].items():
            fi.setdefault(c, []).append([dn, v[0], v[1]])
    fund = {"date": days[-1] if days else "", "rym": yms[-1] if yms else "", "s": s}
    os.makedirs("out", exist_ok=True)
    with open("out/fund.json", "w", encoding="utf-8") as fo:
        json.dump(fund, fo, ensure_ascii=False, separators=(",", ":"))
    print(f"fund.json：{len(s)} 檔，法人 {len(days)} 天，營收 {len(yms)} 個月", flush=True)
    return fund, rv_hist, fi
