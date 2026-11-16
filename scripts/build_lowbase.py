"""
每天收盤後在 GitHub Actions 執行：
1. 補齊過去約十年的全市場每日行情（證交所 MI_INDEX、櫃買中心 dailyQuotes），存在 cache/hist.json.gz
2. 計算每檔股票的「低檔盤整」指標，輸出 out/lowbase.json 給 App 下載

盤整長度的定義：從最近一個交易日往回推，收盤價的（最高－最低）／最低 一直維持在 X% 以內的交易日數。
X 分別算 10、15、20、25、30%，App 端再讓使用者挑。
"""
import gzip, json, os, sys, time, datetime as dt, urllib.request, urllib.error

UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
HIST = "cache/hist.json.gz"
OUT = "out/lowbase.json"
DAYS_BACK = 3680         # 往回補的日曆天數（約 10 年，支援 10 年盤整）
MAX_FETCH = int(os.environ.get("MAX_FETCH", "1700"))  # 單次最多請求數，避免跑太久
SLEEP = 2.2
THRESH = [10, 15, 20, 25, 30]

TW = dt.timezone(dt.timedelta(hours=8))
today = dt.datetime.now(TW).date()


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # 被擋或暫時錯誤：等一下再試
            print("  retry", i + 1, url[-60:], e, flush=True)
            time.sleep(15 * (i + 1))
    return None


def n(v):
    try:
        s = str(v).replace(",", "").replace("+", "").strip()
        if s in ("", "--", "-", "X", "---"):
            return None
        return float(s)
    except Exception:
        return None


def col(fields, names, default):
    """依欄位名稱找位置（舊資料欄位順序可能不同），找不到就用預設位置"""
    fields = [str(f).replace(" ", "") for f in (fields or [])]
    for nm in names:
        for i, f in enumerate(fields):
            if f == nm:
                return i
    return default


def fetch_twse(d):
    j = get(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=ALLBUT0999&response=json")
    if j is None:
        return None
    if j.get("stat") != "OK":
        return {}  # 休市
    t = next((t for t in j.get("tables", []) if "每日收盤行情" in (t.get("title") or "")), None)
    out = {}
    f = (t or {}).get("fields")
    iv, io, ih, il, ic = col(f, ["成交股數"], 2), col(f, ["開盤價"], 5), col(f, ["最高價"], 6), col(f, ["最低價"], 7), col(f, ["收盤價"], 8)
    for r in (t or {}).get("data", []):
        c = str(r[0]).strip()
        if len(c) != 4 or not c.isdigit() or len(r) <= max(iv, io, ih, il, ic):
            continue
        o, h, l, cl, v = n(r[io]), n(r[ih]), n(r[il]), n(r[ic]), n(r[iv])
        if cl is None:
            continue
        out[c] = [str(r[1]).strip(), "L", o or cl, h or cl, l or cl, cl, round((v or 0) / 1000)]
    return out


def fetch_tpex(d):
    j = get(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={d:%Y/%m/%d}&id=&response=json")
    if j is None:
        return None
    t = (j.get("tables") or [{}])[0]
    out = {}
    f = t.get("fields")
    ic, io, ih, il, iv = col(f, ["收盤"], 2), col(f, ["開盤"], 4), col(f, ["最高"], 5), col(f, ["最低"], 6), col(f, ["成交股數"], 8)
    for r in t.get("data", []):
        c = str(r[0]).strip()
        if len(c) != 4 or not c.isdigit() or len(r) <= max(iv, io, ih, il, ic):
            continue
        cl, o, h, l, v = n(r[ic]), n(r[io]), n(r[ih]), n(r[il]), n(r[iv])
        if cl is None:
            continue
        out[c] = [str(r[1]).strip(), "O", o or cl, h or cl, l or cl, cl, round((v or 0) / 1000)]
    return out


def load_hist():
    if os.path.exists(HIST):
        with gzip.open(HIST, "rt", encoding="utf-8") as f:
            return json.load(f)
    return {"days": {}, "closed": []}


def save_hist(h):
    os.makedirs("cache", exist_ok=True)
    with gzip.open(HIST, "wt", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, separators=(",", ":"))


def update(h):
    closed = set(h["closed"])
    want = []
    for i in range(DAYS_BACK):
        d = today - dt.timedelta(days=i)
        k = d.isoformat()
        if d.weekday() >= 5 or k in closed or k in h["days"]:
            continue
        want.append(d)
    print(f"需要補 {len(want)} 天", flush=True)
    fetched = 0
    for d in want:  # 新的日期先抓
        if fetched + 2 > MAX_FETCH:
            print("本次請求數到上限，下次再補", flush=True)
            break
        k = d.isoformat()
        a = fetch_twse(d); time.sleep(SLEEP)
        b = fetch_tpex(d); time.sleep(SLEEP)
        fetched += 2
        if a is None or b is None:
            print(" ", k, "抓取失敗，下次重試", flush=True)
            continue
        if not a and not b:
            # 休市；今天、昨天可能只是還沒公布，不要記成休市
            if (today - d).days >= 2:
                closed.add(k)
            print(" ", k, "無資料", flush=True)
            continue
        if (not a or not b) and (today - d).days < 30:
            print(" ", k, "只有一個市場有資料，下次重試", flush=True)
            continue
        if not a or not b:
            print(" ", k, "只有", "上市" if a else "上櫃", "有資料（舊資料缺漏，先用單一市場）", flush=True)
        a.update(b)
        h["days"][k] = a
        print(" ", k, len(a), "檔", flush=True)
        if fetched % 40 == 0:
            h["closed"] = sorted(closed); save_hist(h)
    # 只保留需要的期間
    cut = (today - dt.timedelta(days=DAYS_BACK + 10)).isoformat()
    h["days"] = {k: v for k, v in h["days"].items() if k >= cut}
    h["closed"] = sorted(c for c in closed if c >= cut)
    return h


def trail_len(closes, pct):
    """從最後一天往回，收盤價區間維持在 pct% 以內的交易日數"""
    hi = lo = closes[-1]
    cnt = 0
    for c in reversed(closes):
        hi, lo = max(hi, c), min(lo, c)
        if (hi - lo) / lo * 100 > pct:
            break
        cnt += 1
    return cnt


def fetch_industry():
    """上市、上櫃公司的產業別代碼（App 用來分科技股／傳產股／金融股）"""
    out = {}
    j = get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")
    for r in j or []:
        c, ic = str(r.get("公司代號", "")).strip(), str(r.get("產業別", "")).strip()
        if c and ic:
            out[c] = ic.zfill(2)
    j = get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O")
    for r in j or []:
        c, ic = str(r.get("SecuritiesCompanyCode", "")).strip(), str(r.get("SecuritiesIndustryCode", "")).strip()
        if c and ic:
            out[c] = ic.zfill(2)
    print(f"產業別：{len(out)} 家", flush=True)
    return out


def compute(h, fund=None):
    dates = sorted(h["days"])
    if not dates:
        return None
    series = {}
    for d in dates:
        for c, r in h["days"][d].items():
            series.setdefault(c, []).append(r)
    last = dates[-1]
    ind = fetch_industry()
    items = []
    for c, rs in series.items():
        if len(rs) < 60 or c not in h["days"][last] or c.startswith("00"):  # 排除 ETF
            continue
        name, mkt = rs[-1][0], rs[-1][1]
        cl = [r[5] for r in rs]
        hi = [r[3] for r in rs]
        lo = [r[4] for r in rs]
        vo = [r[6] for r in rs]
        px = cl[-1]
        y_hi, y_lo = max(hi[-250:]), min(lo[-250:])
        pos = (px - y_lo) / (y_hi - y_lo) * 100 if y_hi > y_lo else 50
        dd = (px / y_hi - 1) * 100 if y_hi else 0
        lens = {str(p): trail_len(cl, p) for p in THRESH}
        L = lens["20"]
        base = cl[-max(L, 1):]
        v20 = sum(vo[-20:]) / min(20, len(vo))
        v120 = sum(vo[-120:]) / min(120, len(vo))
        ma20 = sum(cl[-20:]) / min(20, len(cl))
        ma60 = sum(cl[-60:]) / min(60, len(cl))
        prev = cl[-61:-1] if len(cl) > 60 else cl[:-1]
        if lens["30"] < 63:  # 連最寬條件都不到 3 個月，App 不會用到，省流量
            continue
        items.append({
            "c": c, "n": name, "m": mkt, "px": px,
            "ch": round((px / cl[-2] - 1) * 100, 2) if len(cl) > 1 and cl[-2] else 0,
            "len": lens,                                  # 各振幅門檻下的盤整天數
            "pos": round(pos, 1),                         # 一年區間位置 0=最低 100=最高
            "dd": round(dd, 1),                           # 距一年高點 %
            "bhi": max(base), "blo": min(base),           # 20% 門檻下盤整區間的上下緣
            "v20": round(v20), "v120": round(v120),
            "gap": round(abs(ma20 - ma60) / ma60 * 100, 2) if ma60 else 0,  # 均線糾結程度
            "bo": 1 if prev and px > max(prev) else 0,    # 今天收盤突破前 60 日最高
            "nd": len(rs),
            "ic": ind.get(c, ""),                          # 產業別代碼
            **{k: v for k, v in ((fund or {}).get("s", {}).get(c, {})).items() if k in ("pe", "dy", "pb", "rvy", "rvm", "f5", "f20", "fs", "t5", "t20", "ts")},
        })
    return {"date": last, "days": len(dates), "generated": dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M"), "items": items}


def week_key(d):
    x = dt.date.fromisoformat(d)
    return (x - dt.timedelta(days=x.weekday())).isoformat()


def agg(bars, key):
    """把日 K 合成週 K／月 K（日期用該期間最後一個交易日，和 App 一致）"""
    out, cur, k0 = [], None, None
    for b in bars:
        k = key(b[0])
        if k != k0:
            if cur:
                out.append(cur)
            k0, cur = k, list(b)
        else:
            cur[0] = b[0]; cur[2] = max(cur[2], b[2]); cur[3] = min(cur[3], b[3]); cur[4] = b[4]; cur[5] += b[5]
    if cur:
        out.append(cur)
    return out


def write_kfiles(h, rv=None):
    """每檔股票輸出一個 K 線檔（日 K 最近 500 天、週 K 最近 156 週、月 K 最近 120 個月），App 一次下載就能畫圖"""
    dates = sorted(h["days"])
    series = {}
    for d in dates:
        for c, r in h["days"][d].items():
            series.setdefault(c, []).append([d, r[2], r[3], r[4], r[5], r[6]])
    os.makedirs("out/k", exist_ok=True)
    pk = lambda b: [int(b[0].replace("-", "")), round(b[1], 2), round(b[2], 2), round(b[3], 2), round(b[4], 2), int(b[5])]
    n = 0
    for c, bars in series.items():
        if c not in h["days"][dates[-1]]:
            continue  # 已下市或停牌太久
        # 各年份的日 K（App 往前看時才下載）
        os.makedirs(f"out/kd/{c}", exist_ok=True)
        yrs = {}
        for b in bars:
            yrs.setdefault(b[0][:4], []).append(pk(b))
        for y, bs in yrs.items():
            with open(f"out/kd/{c}/{y}.json", "w") as f:
                json.dump(bs, f, separators=(",", ":"))
        res = {"d": [pk(b) for b in bars[-500:]], "y0": int(min(yrs)),
               "rv": (rv or {}).get(c, []),
               "w": [pk(b) for b in agg(bars[-156 * 5 - 25:], week_key)[-156:]],
               "m": [pk(b) for b in agg(bars, lambda d: d[:7])[-120:]]}
        with open(f"out/k/{c}.json", "w") as f:
            json.dump(res, f, separators=(",", ":"))
        n += 1
    print(f"K 線檔：{n} 檔", flush=True)


def main():
    h = load_hist()
    h = update(h)
    save_hist(h)
    fund, rvh = {}, {}
    try:
        import build_fund
        fund, rvh = build_fund.run(sorted(h["days"]))
    except Exception as e:
        print("基本面／籌碼資料失敗（不影響低檔盤整）", e, flush=True)
    res = compute(h, fund)
    if not res:
        print("沒有資料"); sys.exit(1)
    os.makedirs("out", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    write_kfiles(h, rvh)
    print(f"完成：{res['date']}，共 {res['days']} 個交易日，{len(res['items'])} 檔", flush=True)


if __name__ == "__main__":
    main()
