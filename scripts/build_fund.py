"""
基本面與籌碼資料（每日收盤後由 build_lowbase.py 呼叫）
1. 本益比、殖利率、淨值比：證交所 BWIBBU_ALL、櫃買中心本益比分析
2. 每月營收：最新一個月用 OpenAPI，過去月份從公開資訊觀測站補齊，保留 13 個月
3. 三大法人：每天記錄外資、投信買賣超，保留 30 個交易日，算近 5／20 日合計與連買賣天數

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


ROW = re.compile(r"<td[^>]*>\s*(\d{4})\s*</td>\s*<td[^>]*>[^<]*</td>" + r"\s*<td[^>]*>\s*([-\d,.]+)\s*</td>" * 5)


DEAD = set()  # 這次執行中連不上的網站，不再嘗試


def fetch_rev_month(ym):
    """從公開資訊觀測站補過去月份（上市 sii、上櫃 otc，各國內與國外公司）"""
    y, m = int(ym[:4]) - 1911, int(ym[4:])
    out = {}
    for host in ("mopsov.twse.com.tw", "mops.twse.com.tw"):
        if host in DEAD:
            continue
        fails = 0
        for mk in ("sii", "otc"):
            for t in (0, 1):
                b = raw(f"https://{host}/nas/t21/{mk}/t21sc03_{y}_{m}_{t}.html", tries=1, timeout=20)
                time.sleep(SLEEP)
                if not b:
                    fails += 1
                    if fails >= 2 and not out:
                        DEAD.add(host)
                        print("  ", host, "連不上，這次不再嘗試", flush=True)
                        break
                    continue
                for enc in ("big5", "cp950", "utf-8"):
                    try:
                        html = b.decode(enc)
                        break
                    except Exception:
                        html = ""
                for g in ROW.findall(html):
                    v = n(g[1])
                    if v is not None:
                        out[g[0]] = [v, n(g[4]), n(g[5])]
            if host in DEAD:
                break
        if out:
            break
    print(f"  補 {ym} 月營收：{len(out)} 筆", flush=True)
    return out


def update_rev():
    rev = load(REV, {})
    try:
        fetch_rev_latest(rev)
    except Exception as e:
        print("最新月營收失敗", e, flush=True)
    want = ym_list(13)
    t0 = time.time()
    for ym in want:
        if time.time() - t0 > 360 or len(DEAD) >= 2:
            print("  補月營收時間到或網站連不上，剩下的下次再補", flush=True)
            break
        if len(rev.get(ym, {})) < 500:  # 沒有或太少就補
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
def fetch_insti_day(d):
    """回傳 {代號: [外資, 投信, 自營]}（張）；資料還沒公布回傳 None"""
    out = {}
    j = getj(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d.replace('-', '')}&selectType=ALLBUT0999&response=json", 2, 30)
    time.sleep(SLEEP)
    if not j or j.get("stat") != "OK":
        return None
    for r in j.get("data", []):
        c = str(r[0]).strip()
        if re.fullmatch(r"\d{4}", c):
            out[c] = [round(((n(r[4]) or 0) + (n(r[7]) or 0)) / 1000), round((n(r[10]) or 0) / 1000), round((n(r[11]) or 0) / 1000)]
    j = getj(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={d.replace('-', '/')}&response=json", 2, 30)
    time.sleep(SLEEP)
    t = (j or {}).get("tables") or [{}]
    for r in t[0].get("data", []) if t else []:
        c = str(r[0]).strip()
        if re.fullmatch(r"\d{4}", c):
            out[c] = [round((n(r[10]) or 0) / 1000), round((n(r[13]) or 0) / 1000), round((n(r[22]) or 0) / 1000)]
    return out


def update_insti(dates):
    ins = load(INSTI, {})
    want = dates[-25:]
    t0 = time.time()
    for d in want[::-1]:  # 新的日期先抓
        if d in ins:
            continue
        if time.time() - t0 > 480:
            print("  法人資料時間到，剩下的下次再補", flush=True)
            break
        try:
            got = fetch_insti_day(d)
            if got:
                ins[d] = got
                print(f"  法人 {d}：{len(got)} 檔", flush=True)
                save(INSTI, ins)
        except Exception as e:
            print("  法人失敗", d, e, flush=True)
    ins = {k: v for k, v in ins.items() if k >= want[0]}
    save(INSTI, ins)
    return ins


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
    """回傳 (fund, revHist)：fund 給 App 的 fund.json；revHist 給 K 線檔"""
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
            rv_hist.setdefault(c, []).append([int(ym), round(v[0]), v[1], v[2]])
    for c, h in rv_hist.items():
        last = h[-1]
        s.setdefault(c, {}).update({"rv": last[1], "rvm": last[2], "rvy": last[3], "rym": last[0]})
    days = sorted(ins)
    codes = set().union(*[set(ins[d]) for d in days]) if days else set()
    for c in codes:
        f = [ins[d].get(c, [0, 0, 0])[0] for d in days]
        t = [ins[d].get(c, [0, 0, 0])[1] for d in days]
        s.setdefault(c, {}).update({"f5": sum(f[-5:]), "f20": sum(f[-20:]), "fs": streak(f), "t5": sum(t[-5:]), "t20": sum(t[-20:]), "ts": streak(t)})
    fund = {"date": days[-1] if days else "", "rym": yms[-1] if yms else "", "s": s}
    os.makedirs("out", exist_ok=True)
    with open("out/fund.json", "w", encoding="utf-8") as fo:
        json.dump(fund, fo, ensure_ascii=False, separators=(",", ":"))
    print(f"fund.json：{len(s)} 檔，法人 {len(days)} 天，營收 {len(yms)} 個月", flush=True)
    return fund, rv_hist
