"""
10 年回測：把 App「即將起漲」「建議買進」的規則套回過去每一個交易日，
統計之後 20 個交易日的結果（上漲機率、平均報酬、期間最高漲幅），
讓 App 上每個分數區間都能顯示「歷史上這種條件後來怎麼走」。

使用除權息還原後的股價。法人、營收、本益比沒有 10 年的每日歷史，回測不含這些加分項目。
每週執行一次（約幾分鐘），結果存在 cache/bt.json，並輸出 out/bt.json 給 App。
"""
import json, os, time, datetime as dt

TW = dt.timezone(dt.timedelta(hours=8))
H = 20  # 看之後幾個交易日
VER = 3  # 回測程式版本；改版時自動重跑


def frames(series, dates):
    import pandas as pd
    idx = {d: i for i, d in enumerate(dates)}
    cols = [c for c, rows in series.items() if len(rows) >= 300 and not c.startswith("00")]
    import numpy as np
    N, M = len(dates), len(cols)
    C, Hh, L, V = (np.full((N, M), np.nan) for _ in range(4))
    for j, c in enumerate(cols):
        for r in series[c]:
            i = idx[r[0]]
            Hh[i, j], L[i, j], C[i, j], V[i, j] = r[4], r[5], r[6], r[7]
    mk = lambda a: pd.DataFrame(a, index=dates, columns=cols)
    return mk(C), mk(Hh), mk(L), mk(V)


def stats(ret, mx):
    import numpy as np
    ok = ~np.isnan(ret) & ~np.isnan(mx)
    ret, mx = ret[ok], mx[ok]
    if len(ret) == 0:
        return {"n": 0}
    return {"n": int(len(ret)), "win": round(float((ret > 0).mean() * 100), 1), "avg": round(float(ret.mean() * 100), 2),
            "med": round(float(np.median(ret) * 100), 2), "mx": round(float(np.mean(mx) * 100), 2),
            "up10": round(float((mx >= 0.10).mean() * 100), 1)}


def run(series, dates):
    import numpy as np, pandas as pd
    t0 = time.time()
    C, Hh, L, V = frames(series, dates)
    fwd = C.shift(-H) / C - 1                        # 20 日後報酬
    fmx = Hh[::-1].rolling(H, min_periods=H).max()[::-1].shift(-1) / C - 1  # 之後 20 日內最高漲幅
    valid = fwd.notna() & C.notna()
    base = stats(fwd.values[valid.values], fmx.values[valid.values])

    hi250, lo250 = Hh.rolling(250, min_periods=200).max(), L.rolling(250, min_periods=200).min()
    pos = (C - lo250) / (hi250 - lo250) * 100
    m60, m20 = C.rolling(60).mean(), C.rolling(20).mean()
    m60p = m60.shift(5)
    v20 = V.rolling(20).mean().shift(1)
    v120 = V.rolling(120).mean().shift(1)
    ch = C.pct_change() * 100
    below = C < m60
    bv = below.values
    cnt = np.zeros(bv.shape)
    for i in range(1, len(bv)):
        cnt[i] = np.where(bv[i], cnt[i - 1] + 1, 0)
    u60 = pd.DataFrame(cnt, index=C.index, columns=C.columns).shift(1).fillna(0)  # 昨天為止連續在季線下天數

    # ---------- 即將起漲（季線版），和 App 的 riseOf 相同規則（不含法人） ----------
    was = C.shift(1) < m60.shift(1)
    d60 = (m60 - C) / m60 * 100
    crossed = was & (C >= m60)
    cand = (pos <= 60) & ((was & (d60 > 0) & (d60 <= 5)) | crossed) & valid & (v20 >= 100)
    slope = (m60 - m60p) / m60p * 100
    vr = V / v20
    sc = (np.where(crossed, 35, np.where(d60 <= 1, 30, np.where(d60 <= 2, 25, np.where(d60 <= 3.5, 18, 10))))
          + np.where(slope >= 0, 10, np.where(slope > -0.5, 6, 0))
          + np.where(u60 >= 40, 10, np.where(u60 >= 20, 5, 0))
          + np.where(vr >= 2, 20, np.where(vr >= 1.5, 15, np.where(vr >= 1.2, 8, 0)))
          + np.where(ch >= 3, 15, np.where(ch >= 1, 10, np.where(ch > 0, 5, 0)))
          + np.where(C >= m20, 5, 0))
    m = cand.values
    S, R, X = sc[m], fwd.values[m], fmx.values[m]
    rise = {"all": stats(R, X), "buckets": []}
    for lo, hi, lab in ((0, 50, "50° 以下"), (50, 55, "50～54°"), (55, 65, "55～64°"), (65, 75, "65～74°"), (75, 999, "75° 以上")):
        k = (S >= lo) & (S < hi)
        rise["buckets"].append([lab, lo, stats(R[k], X[k])])
    feats = {"今天站上季線": crossed, "季線翻揚": slope >= 0, "季線下 40 天以上": u60 >= 40, "量 2 倍以上": vr >= 2,
             "今日漲 3% 以上": ch >= 3, "站上月線": C >= m20}
    rise["feats"] = []
    for name, f in feats.items():
        fv = f.values[m] if hasattr(f, "values") else f[m]
        fv = fv.astype(bool)
        rise["feats"].append([name, stats(R[fv], X[fv]), stats(R[~fv], X[~fv])])

    # ---------- 建議買進（打底＋轉強，不含籌碼與基本面） ----------
    rng42 = C.rolling(42).max() / C.rolling(42).min() - 1          # 近 42 日（2 個月）收盤振幅
    rng63 = C.rolling(63).max() / C.rolling(63).min() - 1
    rng126 = C.rolling(126).max() / C.rolling(126).min() - 1
    bhi, blo = C.rolling(42).max(), C.rolling(42).min()
    prev60 = C.shift(1).rolling(60).max()
    bo = C > prev60
    gap = (m20 - m60).abs() / m60 * 100
    candB = (rng42 <= 0.20) & (pos <= 50) & (v20 >= 200) & (C <= bhi * 1.08) & (C >= blo * 0.97) & valid
    Lb = np.where(rng126 <= 0.20, 126, np.where(rng63 <= 0.20, 63, 42))
    scB = (np.minimum(Lb / 252, 1) * 15 + np.maximum(0, (33 - pos) / 33 * 10)
           + np.where(bo, 20, np.where(C >= bhi * 0.97, 10, 0))
           + np.where(V.rolling(20).mean() >= v120 * 1.3, 10, 0)
           + np.where(gap <= 2, 5, 0))
    mb = candB.values
    SB, RB, XB = np.asarray(scB)[mb], fwd.values[mb], fmx.values[mb]
    sug = {"all": stats(RB, XB), "buckets": []}
    for lo, hi, lab in ((0, 25, "25 分以下"), (25, 35, "25～34 分"), (35, 45, "35～44 分"), (45, 999, "45 分以上")):
        k = (SB >= lo) & (SB < hi)
        sug["buckets"].append([lab, lo, stats(RB[k], XB[k])])

    # ---------- 用資料校準評分（模型）：2017～2022 學習，2023 之後檢驗 ----------
    model = model60 = None
    try:
        F = {"d60": (C / m60 - 1) * 100, "slope": slope, "m20": (C >= m20).astype(float), "ch": ch, "vr": vr,
             "u60": u60, "pos": pos, "r10": (C / C.shift(10) - 1) * 100, "r60": (C / C.shift(60) - 1) * 100,
             "dd": (C / hi250 - 1) * 100, "rng42": rng42 * 100, "bo": bo.astype(float), "gap": gap}
        model = fit_model(F, fwd, fmx, (pos <= 60) & (v20 >= 100) & valid, dates)
        fwd60 = C.shift(-60) / C - 1
        fmx60 = Hh[::-1].rolling(60, min_periods=60).max()[::-1].shift(-1) / C - 1
        model60 = fit_model(F, fwd60, fmx60, (pos <= 60) & (v20 >= 100) & fwd60.notna() & C.notna(), dates)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("模型校準失敗", e, flush=True)

    out = {"asof": dates[-1], "from": dates[250] if len(dates) > 250 else dates[0], "days": H,
           "base": base, "rise": rise, "sug": sug, "model": model, "model60": model60, "ver": VER, "secs": round(time.time() - t0)}
    print(f"回測完成：{out['from']}～{out['asof']}，全市場 {base['n']} 筆，即將起漲候選 {rise['all']['n']} 筆，建議買進候選 {sug['all']['n']} 筆，{out['secs']} 秒", flush=True)
    return out


FEAT_NAME = {"d60": "離季線", "slope": "季線方向", "m20": "站上月線", "ch": "今日漲跌", "vr": "今日量／20日均量", "u60": "季線下天數",
             "pos": "一年位置", "r10": "近10日漲跌", "r60": "近60日漲跌", "dd": "距一年高點", "rng42": "近2月振幅", "bo": "創60日新高", "gap": "月季線差距"}


def fit_model(F, fwd, fmx, U, dates, split="2023-01-01"):
    """把每個條件分成 5 段（是／否的條件分 2 段），看學習期間每段之後 20 天的平均報酬比整體高多少，當作那一段的分數；
    總分＝各條件分數加總。再用沒參與學習的檢驗期間，看高分股票是不是真的比較好。"""
    import numpy as np
    mask = U.values
    di = np.array(dates)
    rows = np.where(mask)
    tr = di[rows[0]] < split
    y = fwd.values[mask]
    ymx = fmx.values[mask]
    ok = ~np.isnan(y)
    X = {k: v.values[mask] for k, v in F.items()}
    ytr = y[tr & ok]
    mu = float(np.mean(ytr))
    feats, score = [], np.zeros(len(y))
    for k, x in X.items():
        good = ~np.isnan(x)
        xt = x[tr & ok & good]
        uniq = np.unique(xt)
        edges = [0.5] if len(uniq) <= 2 else sorted(set(np.round(np.quantile(xt, [0.2, 0.4, 0.6, 0.8]), 4).tolist()))
        b = np.searchsorted(edges, x, side="right")
        pts = []
        for i in range(len(edges) + 1):
            sel = tr & ok & good & (b == i)
            nb = int(sel.sum())
            d = (float(np.mean(y[sel])) - mu) * 100 if nb else 0.0
            pts.append(round(d * nb / (nb + 5000), 3))   # 樣本少的段落分數打折，避免過度相信
        score += np.where(good, np.array(pts)[b], 0)
        feats.append({"k": k, "name": FEAT_NAME.get(k, k), "e": edges, "p": pts})
    q = np.quantile(score[tr & ok], np.linspace(0, 1, 101)).round(4).tolist()
    pr = np.searchsorted(q, score, side="right") - 1          # 分數→百分位（0～100）
    te = (~tr) & ok
    def st(sel):
        r, m = y[sel], ymx[sel]
        m = m[~np.isnan(m)]
        return {"n": int(sel.sum()), "win": round(float((r > 0).mean() * 100), 1), "avg": round(float(r.mean() * 100), 2),
                "med": round(float(np.median(r) * 100), 2), "mx": round(float(m.mean() * 100), 2) if len(m) else None,
                "up10": round(float((m >= 0.10).mean() * 100), 1) if len(m) else None} if sel.sum() else {"n": 0}
    dec = []
    for lo in range(0, 100, 10):
        dec.append([lo, st(te & (pr >= lo) & (pr < lo + 10))])
    res = {"feats": feats, "q": q, "split": split, "train": [min(di[rows[0]][tr].tolist()), max(di[rows[0]][tr].tolist())],
           "test": [min(di[rows[0]][~tr].tolist()), max(di[rows[0]][~tr].tolist())], "test_base": st(te), "test_dec": dec,
           "test_top": st(te & (pr >= 90)), "test_top5": st(te & (pr >= 95)), "test_bot": st(te & (pr < 10))}
    print("模型檢驗（2023 之後）：全部 上漲 %s%% 平均 %s%%｜前 10%% 上漲 %s%% 平均 %s%%｜後 10%% 上漲 %s%% 平均 %s%%" % (
        res["test_base"]["win"], res["test_base"]["avg"], res["test_top"]["win"], res["test_top"]["avg"], res["test_bot"]["win"], res["test_bot"]["avg"]), flush=True)
    return res


def maybe_run(series, dates, force=False):
    """一週跑一次；結果存 cache/bt.json，每次都輸出到 out/bt.json"""
    p = "cache/bt.json"
    old = None
    if os.path.exists(p):
        try:
            old = json.load(open(p, encoding="utf-8"))
        except Exception:
            old = None
    stale = force or not old or old.get("ver") != VER or (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(old["asof"])).days >= 7
    if stale:
        try:
            old = run(series, dates)
            os.makedirs("cache", exist_ok=True)
            json.dump(old, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("回測失敗（不影響其他資料）", e, flush=True)
    if old:
        os.makedirs("out", exist_ok=True)
        json.dump(old, open("out/bt.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))


def score_today(series, dates, fund=None, ind=None, top=150):
    """用回測校準好的模型（20 日），幫最新一個交易日全市場低檔區股票打分數。
    回傳 {代號: 分數百分位}，並輸出 out/ai.json（分數最高的股票清單）。"""
    import bisect, json
    try:
        bt = json.load(open("cache/bt.json", encoding="utf-8"))
        m = bt.get("model")
    except Exception:
        m = None
    if not m:
        return {}
    last = dates[-1]
    feats = {f["k"]: f for f in m["feats"]}
    out, rows = {}, []
    for c, rs in series.items():
        if c.startswith("00") or len(rs) < 260 or rs[-1][0] != last:
            continue
        C = [r[6] for r in rs]; Hh = [r[4] for r in rs]; L = [r[5] for r in rs]; V = [r[7] for r in rs]
        px = C[-1]
        m60 = sum(C[-60:]) / 60; m20 = sum(C[-20:]) / 20; m60p = sum(C[-65:-5]) / 60
        hi250, lo250 = max(Hh[-250:]), min(L[-250:])
        if hi250 <= lo250 or m60 <= 0:
            continue
        pos = (px - lo250) / (hi250 - lo250) * 100
        v20 = sum(V[-21:-1]) / 20
        if pos > 60 or v20 < 100:
            continue
        u60 = 0
        for k in range(len(C) - 2, len(C) - 250, -1):
            if C[k] < sum(C[k - 59:k + 1]) / 60:
                u60 += 1
            else:
                break
        x = {"d60": (px / m60 - 1) * 100, "slope": (m60 - m60p) / m60p * 100, "m20": 1.0 if px >= m20 else 0.0,
             "ch": (px / C[-2] - 1) * 100, "vr": V[-1] / v20 if v20 else 0, "u60": u60, "pos": pos,
             "r10": (px / C[-11] - 1) * 100, "r60": (px / C[-61] - 1) * 100, "dd": (px / hi250 - 1) * 100,
             "rng42": (max(C[-42:]) / min(C[-42:]) - 1) * 100, "bo": 1.0 if px > max(C[-61:-1]) else 0.0,
             "gap": abs(m20 - m60) / m60 * 100}
        sc, pts = 0.0, []
        for k, v in x.items():
            f = feats.get(k)
            if not f:
                continue
            p = f["p"][bisect.bisect_right(f["e"], v)]
            sc += p
            pts.append([f["name"], round(p, 2), round(v, 2)])
        pr = max(0, min(100, bisect.bisect_right(m["q"], sc) - 1))
        out[c] = pr
        rows.append({"c": c, "n": rs[-1][1], "m": rs[-1][2], "px": round(px, 2), "ch": round(x["ch"], 2), "ai": pr,
                     "raw": round(sc, 2), "ic": (ind or {}).get(c, ""), "pts": sorted(pts, key=lambda z: -z[1])[:6],
                     "v20": round(v20), "pos": round(pos, 1), "d60": round(x["d60"], 1), "r60": round(x["r60"], 1), "dd": round(x["dd"], 1),
                     **{k: v for k, v in ((fund or {}).get("s", {}).get(c, {})).items() if k in ("pe", "dy", "pb", "rvy", "fs", "ts")}})
    rows.sort(key=lambda r: -r["raw"])
    res = {"date": last, "model_test": m.get("test"), "dec": m.get("test_dec"), "base": m.get("test_base"),
           "top5": m.get("test_top5"), "items": rows[:top], "n": len(rows)}
    os.makedirs("out", exist_ok=True)
    json.dump(res, open("out/ai.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"數據評分：{len(rows)} 檔，精選前 {min(top, len(rows))} 檔", flush=True)
    return out
