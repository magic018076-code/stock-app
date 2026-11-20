"""
美元／新台幣匯率（數字變大＝台幣貶值，變小＝台幣升值）
- 主要：中央銀行開放資料 BP01D01（1993 年起每日 16:00 銀行間匯率）
- 備援：美國聯準會 FRED DEXTAUS（約晚一週更新）
- 最近：期交所每日外幣參考匯率（補上 FRED 還沒更新的日子）
存在 cache/fx.json：{日期: 匯率}；輸出 out/fx.json（近 250 天和升貶幅度）
"""
import datetime as dt, json, os, time, urllib.parse, urllib.request

UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
FX = "cache/fx.json"
TW = dt.timezone(dt.timedelta(hours=8))


def _get(url, data=None, timeout=40, ua=UA):
    for i in range(4):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            print("  匯率 retry", url[:60], e, flush=True)
            time.sleep(10 + i * 10)
    return None


def fetch_fred():
    b = _get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXTAUS&cosd=2006-01-01", timeout=60, ua="curl/8.5.0")   # 只抓 2006 年起；用瀏覽器 UA 會被擋到逾時
    out = {}
    for line in (b or b"").decode("utf-8", "replace").splitlines()[1:]:
        p = line.split(",")
        if len(p) == 2 and p[0] >= "2006-01-01":
            try:
                out[p[0]] = float(p[1])
            except ValueError:
                pass
    return out


def fetch_cbc():
    b = _get("https://cpx.cbc.gov.tw/API/DataAPI/Get?FileName=BP01D01", timeout=60, ua="curl/8.5.0")
    out = {}
    try:
        j = json.loads(b or b"{}")
        d = j.get("data") or []
        rows = d.get("dataSets") if isinstance(d, dict) else d
        for r in rows or []:
            if isinstance(r, list) and len(r) > 1 and len(str(r[0])) == 8 and str(r[0]) >= "20060101":
                try:
                    k = str(r[0])
                    out[f"{k[:4]}-{k[4:6]}-{k[6:]}"] = float(r[1])
                except ValueError:
                    pass
    except Exception as e:
        print("  央行匯率解析失敗", e, flush=True)
    return out


def fetch_taifex(days=60):
    end = dt.datetime.now(TW).date()
    start = end - dt.timedelta(days=days)
    data = urllib.parse.urlencode({"queryStartDate": f"{start:%Y/%m/%d}", "queryEndDate": f"{end:%Y/%m/%d}"}).encode()
    b = _get("https://www.taifex.com.tw/cht/3/dailyFXRateDown", data)
    out = {}
    for line in (b or b"").decode("big5", "replace").splitlines()[1:]:
        p = line.split(",")
        if len(p) >= 2 and len(p[0]) == 10:
            try:
                out[p[0].replace("/", "-")] = float(p[1])
            except ValueError:
                pass
    return out


def run():
    db = {}
    if os.path.exists(FX):
        try:
            db = json.load(open(FX, encoding="utf-8"))
        except Exception:
            db = {}
    f = fetch_fred()
    c = fetch_cbc()
    t = fetch_taifex()
    db.update(f)
    db.update(c)            # 央行優先
    for k, v in t.items():  # 期交所只補央行還沒有的日子
        if k not in c:
            db[k] = v
    os.makedirs("cache", exist_ok=True)
    json.dump(db, open(FX, "w", encoding="utf-8"), separators=(",", ":"))
    days = sorted(db)
    print(f"匯率：{len(days)} 天（央行 {len(c)}、FRED {len(f)}、期交所 {len(t)}），最新 {days[-1] if days else '無'} {db[days[-1]] if days else ''}", flush=True)
    if len(days) > 61:
        v = [db[d] for d in days]
        ch = lambda k: round((v[-1] / v[-1 - k] - 1) * 100, 2)
        out = {"date": days[-1], "rate": v[-1], "c5": ch(5), "c20": ch(20), "c60": ch(60),
               "h": [[int(d.replace("-", "")), db[d]] for d in days[-250:]]}
        os.makedirs("out", exist_ok=True)
        json.dump(out, open("out/fx.json", "w", encoding="utf-8"), separators=(",", ":"))
    return db
