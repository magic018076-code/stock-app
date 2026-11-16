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
    ok = ~np.isnan(ret)
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

    out = {"asof": dates[-1], "from": dates[250] if len(dates) > 250 else dates[0], "days": H,
           "base": base, "rise": rise, "sug": sug, "secs": round(time.time() - t0)}
    print(f"回測完成：{out['from']}～{out['asof']}，全市場 {base['n']} 筆，即將起漲候選 {rise['all']['n']} 筆，建議買進候選 {sug['all']['n']} 筆，{out['secs']} 秒", flush=True)
    return out


def maybe_run(series, dates, force=False):
    """一週跑一次；結果存 cache/bt.json，每次都輸出到 out/bt.json"""
    p = "cache/bt.json"
    old = None
    if os.path.exists(p):
        try:
            old = json.load(open(p, encoding="utf-8"))
        except Exception:
            old = None
    stale = force or not old or (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(old["asof"])).days >= 7
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
