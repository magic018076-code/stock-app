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
VER = 15  # 回測程式版本；改版時自動重跑


def frames(series, dates):
    import pandas as pd
    idx = {d: i for i, d in enumerate(dates)}
    cols = [c for c, rows in series.items() if len(rows) >= 300 and not c.startswith("00")]
    import numpy as np
    N, M = len(dates), len(cols)
    C, Hh, L, V, O = (np.full((N, M), np.nan) for _ in range(5))
    for j, c in enumerate(cols):
        for r in series[c]:
            i = idx[r[0]]
            O[i, j], Hh[i, j], L[i, j], C[i, j], V[i, j] = r[3], r[4], r[5], r[6], r[7]
    mk = lambda a: pd.DataFrame(a, index=dates, columns=cols)
    return mk(C), mk(Hh), mk(L), mk(V), mk(O)


def rev_frame(C):
    """每個交易日當時「已經公布」的最新月營收年增率（每月 10 日前公布上個月營收，所以 11 日起才用上個月的）"""
    import gzip, numpy as np, pandas as pd
    try:
        with gzip.open("cache/rev.json.gz", "rt", encoding="utf-8") as f:
            rev = json.load(f)
    except Exception:
        return pd.DataFrame(np.nan, index=C.index, columns=C.columns)
    out = np.full(C.shape, np.nan)
    cols = {c: j for j, c in enumerate(C.columns)}
    keymap = {}
    for i, d in enumerate(C.index):
        y, m, day = int(d[:4]), int(d[5:7]), int(d[8:10])
        back = 1 if day >= 11 else 2
        m -= back
        while m <= 0:
            m += 12; y -= 1
        keymap.setdefault(f"{y}{m:02d}", []).append(i)
    for ym, idx in keymap.items():
        mon = rev.get(ym)
        if not mon:
            continue
        for c, v in mon.items():
            j = cols.get(c)
            if j is not None and v[2] is not None:
                out[idx, j] = max(-100.0, min(300.0, v[2]))
    return pd.DataFrame(out, index=C.index, columns=C.columns)


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
    C, Hh, L, V, O = frames(series, dates)
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

    # ---------- 突破研究：各種「突破」型態照實際操作賺不賺 ----------
    brk = None
    try:
        brk = momentum_plan(O, Hh, L, C, V, dates)
        try:
            brk["valid"] = brk_validate(O, Hh, L, C, V, dates, series)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("突破驗證失敗", e, flush=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("突破研究失敗", e, flush=True)

    # ---------- 量縮研究：量縮後會不會比較容易漲？ ----------
    vol_study = None
    try:
        fwd60 = C.shift(-60) / C - 1
        fmx60 = Hh[::-1].rolling(60, min_periods=60).max()[::-1].shift(-1) / C - 1
        vq = V.rolling(20).mean() / v120                     # 近月均量 ÷ 半年均量（<1 量縮）
        rng20 = C.rolling(20).max() / C.rolling(20).min() - 1   # 近 20 日收盤振幅（價穩）
        Ubase = (pos <= 60) & (v20 >= 100) & valid & fwd60.notna()
        def g(name, cond):
            m = (Ubase & cond).values
            return [name, stats(fwd.values[m], fmx.values[m]), stats(fwd60.values[m], fmx60.values[m])]
        shrink = vq < 0.7
        vol_study = {"rows": [
            g("低檔區全部（比較基準）", Ubase),
            g("近月量 < 半年量 5 成（明顯量縮）", vq < 0.5),
            g("近月量為半年量 5～7 成（量縮）", (vq >= 0.5) & (vq < 0.7)),
            g("近月量為半年量 7～10 成", (vq >= 0.7) & (vq < 1.0)),
            g("近月量為半年量 1～1.5 倍（量增）", (vq >= 1.0) & (vq < 1.5)),
            g("近月量 > 半年量 1.5 倍（明顯量增）", vq >= 1.5),
            g("量縮＋價穩（近 20 日振幅 8% 內）", shrink & (rng20 <= 0.08)),
            g("量縮後，今天帶量（1.5 倍）上漲 2% 以上", shrink & (vr >= 1.5) & (ch >= 2)),
            g("量縮＋價穩後，今天帶量突破 60 日高點", shrink.shift(1) & (rng20.shift(1) <= 0.10) & bo & (vr >= 1.5)),
            g("量縮＋價穩＋站上季線", shrink & (rng20 <= 0.08) & (C >= m60)),
        ]}
        for r in vol_study["rows"]:
            print("  量縮研究｜%-24s 20日：n=%s 上漲 %s%% 平均 %s%%｜60日：上漲 %s%% 平均 %s%%" % (r[0], r[1].get("n"), r[1].get("win"), r[1].get("avg"), r[2].get("win"), r[2].get("avg")), flush=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("量縮研究失敗", e, flush=True)

    # ---------- 用資料校準評分（模型 v2）：更多條件＋逐年檢驗 ----------
    model = model60 = plan = None
    try:
        rvy = rev_frame(C)                                   # 當時已公布的最新月營收年增率
        above = (C > m60)
        breadth = (above.sum(axis=1) / C.notna().sum(axis=1) * 100)  # 全市場站上季線的比例（大盤多空）
        mkt = pd.DataFrame(np.repeat(breadth.values[:, None], C.shape[1], axis=1), index=C.index, columns=C.columns)
        F = {"d60": (C / m60 - 1) * 100, "slope": slope, "m20": (C >= m20).astype(float), "ch": ch, "vr": vr,
             "u60": u60, "pos": pos, "r5": (C / C.shift(5) - 1) * 100, "r10": (C / C.shift(10) - 1) * 100,
             "r60": (C / C.shift(60) - 1) * 100, "dd": (C / hi250 - 1) * 100, "rng42": rng42 * 100, "bo": bo.astype(float),
             "gap": gap, "vol20": C.pct_change().rolling(20).std() * 100, "amt": (C * V.rolling(20).mean() / 1e5),
             "rvy": rvy, "mkt": mkt, "vq": V.rolling(20).mean() / v120}
        jump = C.pct_change().abs().rolling(60, min_periods=1).max() > 0.11   # 單日漲跌超過 11%＝資料斷層（未還原的減資等），排除
        extreme = ((C / hi250 - 1) * 100 < -75) | (rng42 * 100 > 150)   # 極端崩跌股（可能有重大問題），排除
        U = (pos <= 60) & (v20 >= 100) & valid & ~jump & ~extreme
        # 逐年檢驗：每一年都只用「那一年以前」的資料學習，再預測那一年
        y0 = int(dates[0][:4]) + 4
        wf = []
        pool_pr, pool_y, pool_mx = [], [], []
        sig_i, sig_j, sig_s = [], [], []
        for Y in range(y0, int(dates[-1][:4]) + 1):
            r = fit_model(F, fwd, fmx, U, dates, split=f"{Y}-01-01", end=f"{Y + 1}-01-01", quiet=True)
            if r and r.get("_te") is not None:
                pool_pr.append(r["_te"][0]); pool_y.append(r["_te"][1]); pool_mx.append(r["_te"][2])
                sig_i.append(r["_te"][3]); sig_j.append(r["_te"][4]); sig_s.append(r["_te"][5])
                wf.append([Y, r["test_base"], r["test_top"], r["test_top5"]])
                print(f"  逐年檢驗 {Y}：全部 上漲 {r['test_base'].get('win')}% 平均 {r['test_base'].get('avg')}%｜前 10% 上漲 {r['test_top'].get('win')}% 平均 {r['test_top'].get('avg')}%", flush=True)
        # 實際使用的模型：用全部已知結果的資料學習
        model = fit_model(F, fwd, fmx, U, dates, split=dates[-H - 1], quiet=True)
        if pool_pr:
            P, Yr, Mx = np.concatenate(pool_pr), np.concatenate(pool_y), np.concatenate(pool_mx)
            st = lambda sel: _st(Yr, Mx, sel)
            model["test_dec"] = [[lo, st((P >= lo) & (P < lo + 10))] for lo in range(0, 100, 10)]
            model["test_base"], model["test_top"], model["test_top5"], model["test_bot"] = st(P >= 0), st(P >= 90), st(P >= 95), st(P < 10)
            model["test"] = [f"{wf[0][0]}-01", dates[-1][:7]]
            model["wf"] = wf
            print("模型 v2 逐年檢驗合計：全部 上漲 %s%% 平均 %s%%｜前 10%% 上漲 %s%% 平均 %s%%｜前 5%% 上漲 %s%% 平均 %s%%" % (
                model["test_base"]["win"], model["test_base"]["avg"], model["test_top"]["win"], model["test_top"]["avg"], model["test_top5"]["win"], model["test_top5"]["avg"]), flush=True)
        model.pop("_te", None)
        if sig_i:
            try:
                diagnose(O, C, V, np.concatenate(sig_i), np.concatenate(sig_j), np.concatenate(sig_s), np.concatenate(pool_pr), breadth.values, dates)
            except Exception as e:
                import traceback; traceback.print_exc()
            plan = plan_backtest(O, Hh, L, C, V, np.concatenate(sig_i), np.concatenate(sig_j), np.concatenate(sig_s), dates, breadth.values)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("模型校準失敗", e, flush=True)

    out = {"asof": dates[-1], "from": dates[250] if len(dates) > 250 else dates[0], "days": H,
           "base": base, "rise": rise, "sug": sug, "model": model, "model60": model60, "vol_study": vol_study, "plan": plan, "brk": brk, "ver": VER, "secs": round(time.time() - t0)}
    print(f"回測完成：{out['from']}～{out['asof']}，全市場 {base['n']} 筆，即將起漲候選 {rise['all']['n']} 筆，建議買進候選 {sug['all']['n']} 筆，{out['secs']} 秒", flush=True)
    return out


FEAT_NAME = {"vq": "近月量／半年量", "r5": "近5日漲跌", "vol20": "波動度", "amt": "成交金額(億)", "rvy": "營收年增", "mkt": "大盤站上季線比例", "d60": "離季線", "slope": "季線方向", "m20": "站上月線", "ch": "今日漲跌", "vr": "今日量／20日均量", "u60": "季線下天數",
             "pos": "一年位置", "r10": "近10日漲跌", "r60": "近60日漲跌", "dd": "距一年高點", "rng42": "近2月振幅", "bo": "創60日新高", "gap": "月季線差距"}


def _st(y, ymx, sel):
    import numpy as np
    if not sel.sum():
        return {"n": 0}
    r, m = y[sel], ymx[sel]
    m = m[~np.isnan(m)]
    return {"n": int(sel.sum()), "win": round(float((r > 0).mean() * 100), 1), "avg": round(float(r.mean() * 100), 2),
            "med": round(float(np.median(r) * 100), 2), "mx": round(float(m.mean() * 100), 2) if len(m) else None,
            "up10": round(float((m >= 0.10).mean() * 100), 1) if len(m) else None}


def fit_model(F, fwd, fmx, U, dates, split="2023-01-01", end=None, quiet=False):
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
    te = (~tr) & ok & ((di[rows[0]] < end) if end else True)
    st = lambda sel: _st(y, ymx, sel)
    dec = []
    for lo in range(0, 100, 10):
        dec.append([lo, st(te & (pr >= lo) & (pr < lo + 10))])
    res = {"feats": feats, "q": q, "split": split, "train": [min(di[rows[0]][tr].tolist()), max(di[rows[0]][tr].tolist())],
           "test": [min(di[rows[0]][te].tolist()), max(di[rows[0]][te].tolist())] if te.sum() else None, "test_base": st(te), "test_dec": dec,
           "test_top": st(te & (pr >= 90)), "test_top5": st(te & (pr >= 95)), "test_bot": st(te & (pr < 10))}
    res["_te"] = (pr[te], y[te], ymx[te], rows[0][te], rows[1][te], score[te]) if te.sum() else None
    if not quiet:
        print("模型檢驗：全部 上漲 %s%% 平均 %s%%｜前 10%% 上漲 %s%% 平均 %s%%" % (
            res["test_base"].get("win"), res["test_base"].get("avg"), res["test_top"].get("win"), res["test_top"].get("avg")), flush=True)
    return res


COST = 0.00585   # 買賣手續費 0.1425%×2 ＋ 證交稅 0.3%
LIQ = 0.2        # 日均成交金額至少 0.2 億（2,000 萬）才推薦
TIERS = (("s", 0, 3), ("b", 3, 6), ("w", 6, 12))   # 依當天排名：前 3%、3～6%、6～12%
STATES = (("green", 0, 15), ("yellow", 15, 25), ("red", 25, 40), ("black", 40, 101))   # 大盤燈號：全市場站上季線的比例
GO = ("green2", "green", "rebound", "yellow")       # 可以進場的燈號
LIGHTS = ("green2", "green", "rebound", "yellow", "red", "black")


def light_state(b, mn10):
    """大盤燈號。b＝今天全市場站上季線的比例；mn10＝近 10 個交易日的最低值
    green2 綠燈・止跌回升（最佳）｜green 綠燈・仍在探底｜rebound 反彈初期｜yellow 黃燈｜red 觀望｜black 不買"""
    if b < 15:
        return "green2" if b >= mn10 + 2 else "green"
    if mn10 < 15 and b >= mn10 + 3 and b < 30:
        return "rebound"
    if b < 25:
        return "yellow"
    return "red" if b < 40 else "black"
HOLD = 20


def breakout_study(O, Hh, L, C, V, dates):
    """各種突破型態：訊號當天收盤後出現 → 隔天開盤買 → 抱 20 天收盤賣，扣成本。依大盤狀態分開看。"""
    import numpy as np, pandas as pd
    m5, m20, m60, m120 = C.rolling(5).mean(), C.rolling(20).mean(), C.rolling(60).mean(), C.rolling(120).mean()
    hi250, lo250 = Hh.rolling(250, min_periods=200).max(), L.rolling(250, min_periods=200).min()
    pos = (C - lo250) / (hi250 - lo250) * 100
    v20 = V.rolling(20).mean().shift(1)
    vr = V / v20
    ch = C.pct_change() * 100
    amt = C * V.rolling(20).mean() / 1e5
    jump = C.pct_change().abs().rolling(60, min_periods=1).max() > 0.11
    below = (C < m60).values
    cnt = np.zeros(below.shape)
    for i in range(1, len(below)):
        cnt[i] = np.where(below[i], cnt[i - 1] + 1, 0)
    u60 = pd.DataFrame(cnt, index=C.index, columns=C.columns).shift(1).fillna(0)
    slope = (m60 / m60.shift(5) - 1) * 100
    cross = (C >= m60) & (C.shift(1) < m60.shift(1))
    hh20, hh60, hh120 = C.shift(1).rolling(20).max(), C.shift(1).rolling(60).max(), C.shift(1).rolling(120).max()
    rng42p = C.shift(1).rolling(42).max() / C.shift(1).rolling(42).min() - 1
    rng20p = C.shift(1).rolling(20).max() / C.shift(1).rolling(20).min() - 1
    d60 = (C / m60 - 1) * 100
    since = cross.astype(float).rolling(10).sum().shift(1) > 0            # 過去 10 天內曾站上季線
    base = (amt >= LIQ) & ~jump & C.notna() & (ch < 9.5)                   # 有流動性、非資料斷層、當天沒漲停（漲停隔天買不到好價）
    entry = O.shift(-1)
    r20 = C.shift(-HOLD) / entry - 1 - COST
    r10 = C.shift(-10) / entry - 1 - COST
    r5 = C.shift(-5) / entry - 1 - COST
    breadth = ((C > m60).sum(axis=1) / C.notna().sum(axis=1) * 100).values
    S = {
        "全部有流動性股票（基準）": base,
        "低檔區全部（一年位置≤60）": base & (pos <= 60),
        "A 今天站上季線": base & cross,
        "A1 站上季線（之前在線下≥20天）": base & cross & (u60 >= 20),
        "A2 站上季線＋量1.5倍": base & cross & (vr >= 1.5),
        "A3 站上季線＋季線翻揚": base & cross & (slope >= 0),
        "A4 站上季線＋量1.5倍＋漲3%": base & cross & (vr >= 1.5) & (ch >= 3),
        "A5 站上季線＋低檔（位置≤40）": base & cross & (pos <= 40),
        "B 季線下0～3%（即將突破）": base & (d60 < 0) & (d60 >= -3) & (C.shift(1) < m60.shift(1)),
        "B1 季線下0～3%＋站上月線＋季線翻揚": base & (d60 < 0) & (d60 >= -3) & (C >= m20) & (slope >= 0),
        "C 站上季線後10天內回測不破（線上0～3%）": base & since & (d60 >= 0) & (d60 <= 3) & (ch < 0),
        "D 突破60日新高": base & (C > hh60),
        "D1 突破60日新高＋量1.5倍": base & (C > hh60) & (vr >= 1.5),
        "D2 突破60日新高＋低檔（位置≤60）": base & (C > hh60) & (pos <= 60),
        "E 突破120日新高": base & (C > hh120),
        "E1 突破120日新高＋量2倍": base & (C > hh120) & (vr >= 2),
        "F 盤整（2月振幅≤15%）後突破上緣": base & (rng42p <= 0.15) & (C > C.shift(1).rolling(42).max()),
        "F1 盤整突破＋量2倍": base & (rng42p <= 0.15) & (C > C.shift(1).rolling(42).max()) & (vr >= 2),
        "F2 窄幅盤整（20日振幅≤8%）後帶量突破": base & (rng20p <= 0.08) & (C > hh20) & (vr >= 1.5),
        "G 均線多頭排列剛形成（5>20>60）": base & (m5 > m20) & (m20 > m60) & ~((m5.shift(1) > m20.shift(1)) & (m20.shift(1) > m60.shift(1))),
        "H 月線穿越季線（黃金交叉）": base & (m20 > m60) & (m20.shift(1) <= m60.shift(1)),
        "I 強勢股拉回月線（位置≥70、碰月線）": base & (pos >= 70) & (L <= m20) & (C >= m20) & (m20 > m60),
        "J 一年新高": base & (C >= hi250.shift(1)),
    }
    BK = ((0, 25), (25, 40), (40, 60), (60, 101))
    yrs = np.array([int(d[:4]) for d in dates])
    for name, m in S.items():
        mv = m.values & r20.notna().values
        ri = np.where(mv)[0]
        v20_ = r20.values[mv]
        if len(v20_) < 200:
            print(f"  突破｜{name}：樣本太少 {len(v20_)}", flush=True)
            continue
        f = lambda v: "%.1f%%/%+.2f%%" % ((v > 0).mean() * 100, v.mean() * 100) if len(v) >= 100 else "—"
        v10_, v5_ = r10.values[mv], r5.values[mv]
        parts = [f(v20_[(breadth[ri] >= lo) & (breadth[ri] < hi)]) for lo, hi in BK]
        ys = [v20_[yrs[ri] == y].mean() * 100 for y in np.unique(yrs[ri]) if (yrs[ri] == y).sum() >= 50]
        print("  突破｜%-26s n=%7d｜5日 %s｜10日 %s｜20日 %s｜大盤<25 %s｜25-40 %s｜40-60 %s｜>60 %s｜賺錢年 %d/%d 最差年 %+.1f%%" % (
            name, len(v20_), f(v5_[~np.isnan(v5_)]), f(v10_[~np.isnan(v10_)]), f(v20_), parts[0], parts[1], parts[2], parts[3],
            sum(1 for y in ys if y > 0), len(ys), min(ys) if ys else 0), flush=True)


BRK_STATES = ((0, 25), (25, 40), (40, 60), (60, 101))


def brk_signals(Hh, L, C, V):
    """強勢股創新高突破的訊號（回測和每日選股共用同一套定義）"""
    import numpy as np, pandas as pd
    prev = C.shift(1)
    tr = pd.concat([(Hh - L).stack(), (Hh - prev).abs().stack(), (L - prev).abs().stack()], axis=1).max(axis=1).unstack().reindex(index=C.index, columns=C.columns)
    atr = tr.rolling(14).mean()
    hi250 = Hh.rolling(250, min_periods=200).max()
    ch = C.pct_change() * 100
    amt = C * V.rolling(20).mean() / 1e5
    jump = C.pct_change().abs().rolling(60, min_periods=1).max() > 0.11
    base = (amt >= LIQ) & ~jump & C.notna() & (ch < 9.5) & atr.notna()
    nh250 = C >= hi250.shift(1)                               # 收盤創一年新高
    nh120 = C > C.shift(1).rolling(120).max()                 # 收盤創 120 日新高
    fresh = lambda x: x & ~(x.shift(1).rolling(20).max().fillna(0) > 0)   # 前 20 天沒出現過＝剛突破
    m20, m60 = C.rolling(20).mean(), C.rolling(60).mean()
    lo250 = L.rolling(250, min_periods=200).min()
    pos = (C - lo250) / (hi250 - lo250) * 100
    sig = {"J": base & nh250, "Jf": base & fresh(nh250), "E": base & nh120, "Ef": base & fresh(nh120),
           "I": base & (pos >= 70) & (L <= m20) & (C >= m20) & (m20 > m60)}
    breadth = ((C > m60).sum(axis=1) / C.notna().sum(axis=1) * 100)
    return sig, atr, breadth, V / V.rolling(20).mean().shift(1), pos


def momentum_plan(O, Hh, L, C, V, dates):
    """強勢股創新高：測試停損、移動停利、持有天數的組合（隔天開盤買，扣成本）"""
    import numpy as np
    sig, atr, breadth, vr, pos = brk_signals(Hh, L, C, V)
    Cv, Hv, Lv, Ov, Av, br = C.values, Hh.values, L.values, O.values, atr.values, breadth.values
    N = len(dates)
    MAXH = 60
    yrs_all = np.array([int(d[:4]) for d in dates])

    def prep(mask):
        si, sj = np.where(mask.values)
        ok = si + MAXH < N
        si, sj = si[ok], sj[ok]
        win = si[:, None] + np.arange(1, MAXH + 1)[None, :]
        return si, sj, Ov[win, sj[:, None]], Hv[win, sj[:, None]], Lv[win, sj[:, None]], Cv[win, sj[:, None]]

    def sim(P, ks, trail, hold):
        si, sj, Ow, Hw, Lw, Cw = P
        px, a = Cv[si, sj], Av[si, sj]
        entry = Ow[:, 0]
        ok = ~np.isnan(entry) & (entry <= px + a)                  # 開盤跳空超過 1 ATR 不追
        stop = px - ks * a
        alive = ok.copy()
        ex = np.full(len(px), np.nan)
        days = np.zeros(len(px))
        peak = px.copy()
        for d in range(hold):
            lo, op, cl = Lw[:, d], Ow[:, d], Cw[:, d]
            hit = alive & (lo <= stop)
            ex[hit] = np.minimum(stop[hit], op[hit]); days[hit] = d + 1
            alive &= ~hit
            if trail:
                peak = np.where(alive & ~np.isnan(cl), np.maximum(peak, cl), peak)
                stop = np.where(alive, np.maximum(stop, peak - trail * a), stop)
        last = Cw[:, hold - 1]
        for d in range(hold - 2, -1, -1):
            last = np.where(np.isnan(last), Cw[:, d], last)
        ex[alive] = last[alive]; days[alive] = hold
        ret = ex / entry - 1 - COST
        m = ok & ~np.isnan(ret)
        return ret[m], days[m], si[m]

    def summ(ret, days, si):
        if len(ret) < 50:
            return {"n": int(len(ret))}
        w, l = ret[ret > 0], ret[ret <= 0]
        yy = yrs_all[si]
        ys = [float(ret[yy == y].mean() * 100) for y in np.unique(yy) if (yy == y).sum() >= 30]
        d = np.unique(si)
        return {"n": int(len(ret)), "win": round(float((ret > 0).mean() * 100), 1), "avg": round(float(ret.mean() * 100), 2),
                "aw": round(float(w.mean() * 100), 2), "al": round(float(l.mean() * 100), 2), "days": round(float(days.mean()), 1),
                "yp": sum(1 for y in ys if y > 0), "yn": len(ys), "worst": round(min(ys), 2) if ys else None, "ndays": int(len(d))}

    out, best = {"grid": []}, None
    preps = {k: prep(m) for k, m in sig.items()}
    for k in ("Jf", "Ef", "J", "E", "I"):
        for hold in (20, 40, 60):
            for ks in (2.0, 3.0):
                for trail in (0, 3.0, 4.0):
                    st = summ(*sim(preps[k], ks, trail, hold))
                    if st["n"] < 50:
                        continue
                    out["grid"].append([k, hold, ks, trail, st])
                    print("  強勢突破｜%-3s 抱%2d天 停損%.0fATR 移動停利%s｜n=%6d 勝率 %.1f%% 平均 %+.2f%%（賺 %+.1f%%／賠 %+.1f%%）抱 %.0f 天｜賺錢年 %d/%d 最差 %+.1f%%｜每日平均 %+.3f%%" % (
                        k, hold, ks, f"{trail:.0f}ATR" if trail else "無  ", st["n"], st["win"], st["avg"], st["aw"], st["al"], st["days"], st["yp"], st["yn"], st["worst"], st["avg"] / st["days"]), flush=True)
                    key = st["avg"] / st["days"] * 20 + 0.3 * st["worst"] / st["days"] * 20   # 每 20 天的報酬，並看重最差年
                    if k in ("Jf", "Ef") and (best is None or key > best[0]):
                        best = (key, k, hold, ks, trail)
    if best:
        _, k, hold, ks, trail = best
        out.update(sig=k, hold=hold, ks=ks, trail=trail, cost=COST * 100, liq=LIQ)
        P = preps[k]
        ret, days, si = sim(P, ks, trail, hold)
        out["all"] = summ(ret, days, si)
        out["states"] = []
        for lo, hi in BRK_STATES:
            m = (br[si] >= lo) & (br[si] < hi)
            st = summ(ret[m], days[m], si[m])
            out["states"].append([lo, hi, st])
            print(f"  強勢突破 最佳 {k} 抱{hold} 停損{ks} 移動{trail}｜大盤 {lo}～{hi}%：{st}", flush=True)
        yy = yrs_all[si]
        out["years"] = [[int(y), summ(ret[yy == y], days[yy == y], si[yy == y])] for y in np.unique(yy)]
        for y, st in out["years"]:
            print(f"     {y}：{st}", flush=True)
        # 帶量與否
        vv = vr.values[P[0], P[1]]
        okm = ~np.isnan(P[2][:, 0]) & (P[2][:, 0] <= Cv[P[0], P[1]] + Av[P[0], P[1]])
        vv = vv[okm][: len(ret)] if okm.sum() == len(ret) else None
        if vv is not None:
            for lo, hi in ((0, 1), (1, 2), (2, 99)):
                m = (vv >= lo) & (vv < hi)
                print(f"     量 {lo}～{hi} 倍：{summ(ret[m], days[m], si[m])}", flush=True)
    return out


def brk_validate(O, Hh, L, C, V, dates, series):
    """用大數據檢驗「即將突破」：對照組、逐年挑參數、抽樣可信範圍、程式一致性、觀察名單的突破機率"""
    import numpy as np
    rng = np.random.default_rng(7)
    sig, atr, breadth, vr, pos = brk_signals(Hh, L, C, V)
    Cv, Hv, Lv, Ov, Av = C.values, Hh.values, L.values, O.values, atr.values
    N, MAXH = len(dates), 60
    yrs_all = np.array([int(d[:4]) for d in dates])
    mon_all = np.array([d[:7] for d in dates])

    def prep(si, sj):
        ok = si + MAXH < N
        si, sj = si[ok], sj[ok]
        win = si[:, None] + np.arange(1, MAXH + 1)[None, :]
        return si, sj, Ov[win, sj[:, None]], Lv[win, sj[:, None]], Cv[win, sj[:, None]]

    def sim(P, ks, trail, hold):
        si, sj, Ow, Lw, Cw = P
        px, a = Cv[si, sj], Av[si, sj]
        entry = Ow[:, 0]
        ok = ~np.isnan(entry) & (entry <= px + a)
        stop = px - ks * a
        alive, ex, peak = ok.copy(), np.full(len(px), np.nan), px.copy()
        for d in range(hold):
            hit = alive & (Lw[:, d] <= stop)
            ex[hit] = np.minimum(stop[hit], Ow[hit, d])
            alive &= ~hit
            if trail:
                peak = np.where(alive & ~np.isnan(Cw[:, d]), np.maximum(peak, Cw[:, d]), peak)
                stop = np.where(alive, np.maximum(stop, peak - trail * a), stop)
        last = Cw[:, hold - 1]
        for d in range(hold - 2, -1, -1):
            last = np.where(np.isnan(last), Cw[:, d], last)
        ex[alive] = last[alive]
        ret = ex / entry - 1 - COST
        m = ok & ~np.isnan(ret)
        return ret[m], si[m]

    out = {}
    PJ = prep(*np.where(sig["Jf"].values))
    ret, si = sim(PJ, 3.0, 0, 60)
    # 1. 對照組：同樣規則套在隨機抽的有流動性股票
    base = (sig["J"] | ~sig["J"]) & (C * V.rolling(20).mean() / 1e5 >= LIQ) & atr.notna() & C.notna()
    bi, bj = np.where(base.values)
    pick = rng.choice(len(bi), size=min(len(bi), 200000), replace=False)
    rb, sb = sim(prep(bi[pick], bj[pick]), 3.0, 0, 60)
    print("  驗證1 對照組｜創一年新高：%d 筆 勝率 %.1f%% 平均 %+.2f%%｜隨機有流動性股票：%d 筆 勝率 %.1f%% 平均 %+.2f%%｜差距 %+.2f%%" % (
        len(ret), (ret > 0).mean() * 100, ret.mean() * 100, len(rb), (rb > 0).mean() * 100, rb.mean() * 100, (ret.mean() - rb.mean()) * 100), flush=True)
    out["bench"] = {"sig": round(float(ret.mean() * 100), 2), "rand": round(float(rb.mean() * 100), 2), "rand_win": round(float((rb > 0).mean() * 100), 1)}
    ys = []
    for y in np.unique(yrs_all[si]):
        a, b = ret[yrs_all[si] == y], rb[yrs_all[sb] == y]
        if len(a) >= 30 and len(b) >= 30:
            ys.append([int(y), round(float(a.mean() * 100), 2), round(float(b.mean() * 100), 2)])
            print("     %d：創新高 %+.2f%%｜隨機 %+.2f%%｜差距 %+.2f%%" % (y, a.mean() * 100, b.mean() * 100, (a.mean() - b.mean()) * 100), flush=True)
    out["bench_years"] = ys
    out["beat_years"] = [sum(1 for y in ys if y[1] > y[2]), len(ys)]
    # 2. 逐年挑參數（不偷看當年）
    combos = [(h, k, t) for h in (20, 40, 60) for k in (2.0, 3.0) for t in (0, 3.0, 4.0)]
    res = {c: sim(PJ, c[1], c[2], c[0]) for c in combos}
    wf_r, wf_days = [], []
    for Y in range(int(dates[0][:4]) + 3, int(dates[-1][:4]) + 1):
        cut = np.searchsorted(dates, f"{Y}-01-01") - MAXH        # 只用當年以前、而且已經出場的交易
        best, bk = None, None
        for c, (r, s_) in res.items():
            m = s_ < cut
            if m.sum() < 200:
                continue
            key = r[m].mean() / c[0]
            if best is None or key > best:
                best, bk = key, c
        if bk is None:
            continue
        r, s_ = res[bk]
        m = yrs_all[s_] == Y
        if not m.sum():
            continue
        wf_r.append(r[m]); wf_days.append(s_[m])
        print(f"  驗證2 逐年挑參數｜{Y}：用之前資料選到 抱{bk[0]}天 停損{bk[1]}ATR 移動{bk[2] or '無'} → 當年 {m.sum()} 筆 勝率 {(r[m] > 0).mean() * 100:.1f}% 平均 {r[m].mean() * 100:+.2f}%", flush=True)
    if wf_r:
        w = np.concatenate(wf_r)
        out["wf"] = {"n": int(len(w)), "win": round(float((w > 0).mean() * 100), 1), "avg": round(float(w.mean() * 100), 2)}
        print(f"  驗證2 合計：{out['wf']}", flush=True)
    # 3. 抽樣可信範圍（以月為單位重抽，因為同一段時間的交易會一起漲跌）
    def boot(r, s_, r2=None, s2=None):
        ms = np.unique(mon_all[s_])
        g = {m: r[mon_all[s_] == m] for m in ms}
        g2 = {m: r2[mon_all[s2] == m] for m in ms} if r2 is not None else None
        vals = []
        for _ in range(1000):
            pk = rng.choice(ms, size=len(ms), replace=True)
            a = np.concatenate([g[m] for m in pk])
            v = a.mean()
            if g2 is not None:
                b = [g2[m] for m in pk if len(g2[m])]
                v -= np.concatenate(b).mean() if b else 0
            vals.append(v * 100)
        return [round(float(np.percentile(vals, q)), 2) for q in (2.5, 50, 97.5)], round(float((np.array(vals) > 0).mean() * 100), 1)
    ci, p1 = boot(ret, si)
    ci2, p2 = boot(ret, si, rb, sb)
    out["ci"], out["p_pos"], out["ci_excess"], out["p_excess"] = ci, p1, ci2, p2
    print(f"  驗證3 抽樣｜平均每筆 95% 可信範圍 {ci[0]:+.2f}%～{ci[2]:+.2f}%（大於 0 的機率 {p1}%）｜贏過隨機的幅度 {ci2[0]:+.2f}%～{ci2[2]:+.2f}%（贏的機率 {p2}%）", flush=True)
    # 4. 程式一致性：每日名單用的逐檔算法 vs 回測用的矩陣算法（最近 250 個交易日）
    vec = set()
    jf = sig["Jf"].values
    cols = list(C.columns)
    for i in range(N - 250, N):
        for j in np.flatnonzero(jf[i]):
            vec.add((dates[i], cols[j]))
    py = set()
    d0 = dates[N - 250]
    for c in cols:
        rs = series[c]
        Cc = [r[6] for r in rs]; Hc = [r[4] for r in rs]; Vc = [r[7] for r in rs]
        n = len(Cc)
        k0 = next((k for k, r in enumerate(rs) if r[0] >= d0), n)
        nh = {}
        for k in range(max(251, k0 - 21), n):
            nh[k] = Cc[k] >= max(Hc[k - 250:k])
        for k in range(max(271, k0), n):
            if not nh.get(k) or any(nh.get(j) for j in range(k - 20, k)):
                continue
            if (Cc[k] / Cc[k - 1] - 1) * 100 >= 9.5 or Cc[k] * sum(Vc[k - 19:k + 1]) / 20 / 1e5 < LIQ:
                continue
            if any(abs(Cc[i] / Cc[i - 1] - 1) > 0.11 for i in range(k - 59, k + 1)):
                continue
            py.add((rs[k][0], c))
    both = len(vec & py)
    out["consist"] = {"vec": len(vec), "py": len(py), "both": both}
    print(f"  驗證4 一致性｜近 250 日：回測算法 {len(vec)} 筆、每日名單算法 {len(py)} 筆、兩邊相同 {both} 筆（{both / max(len(vec | py), 1) * 100:.1f}%）", flush=True)
    # 5. 觀察名單：離一年新高 3% 內、近 20 天沒創新高 → 之後突破的機率；提前買 vs 等突破
    hi250 = Hh.rolling(250, min_periods=200).max().shift(1)
    nh = (C >= hi250)
    recent_nh = nh.shift(1).rolling(20).max().fillna(0) > 0
    dist = (hi250 / C - 1) * 100
    near = (dist > 0) & (dist <= 3) & ~recent_nh & (C * V.rolling(20).mean() / 1e5 >= LIQ) & atr.notna()
    fut = lambda k: nh[::-1].rolling(k, min_periods=1).max()[::-1].shift(-1).fillna(0) > 0
    nm = near.values
    for k in (5, 10, 20):
        print(f"  驗證5 觀察名單｜{k} 天內突破的機率 {fut(k).values[nm].mean() * 100:.1f}%", flush=True)
    out["near_p"] = {k: round(float(fut(k).values[nm].mean() * 100), 1) for k in (5, 10, 20)}
    ni, nj = np.where(nm)
    pk = rng.choice(len(ni), size=min(len(ni), 100000), replace=False)
    rn, sn = sim(prep(ni[pk], nj[pk]), 3.0, 0, 60)
    out["near_buy"] = {"n": int(len(rn)), "win": round(float((rn > 0).mean() * 100), 1), "avg": round(float(rn.mean() * 100), 2)}
    print(f"  驗證5 提前買（還沒突破就買）：{out['near_buy']}｜等突破再買：平均 {ret.mean() * 100:+.2f}%", flush=True)
    for lo, hi in ((0, 1), (1, 2), (2, 3)):
        m = (dist.values[ni[pk], nj[pk]] > lo) & (dist.values[ni[pk], nj[pk]] <= hi)
        okm = ~np.isnan(Ov[np.minimum(ni[pk] + 1, N - 1), nj[pk]])
    return out


def diagnose(O, C, V, si, sj, ss, spr, breadth, dates):
    """找出優勢在哪個環節消失：流動性、隔天開盤進場、成本、每天選 vs 只在超跌時選"""
    import numpy as np, pandas as pd
    Cv, Ov = C.values, O.values
    amt = (C * V.rolling(20).mean() / 1e5).values[si, sj]
    N = len(dates)
    ok = si + HOLD < N
    si, sj, ss, spr, amt = si[ok], sj[ok], ss[ok], spr[ok], amt[ok]
    cc = Cv[si + HOLD, sj] / Cv[si, sj] - 1
    oc = Cv[si + HOLD, sj] / Ov[si + 1, sj] - 1 - COST
    gapo = Ov[si + 1, sj] / Cv[si, sj] - 1
    br = breadth[si]
    yrs = np.array([int(dates[i][:4]) for i in si])

    def drank(mask):
        idx = np.flatnonzero(mask)
        o = idx[np.lexsort((-ss[idx], si[idx]))]
        d = si[o]
        st = np.r_[0, np.flatnonzero(np.diff(d)) + 1]
        cnt = np.diff(np.r_[st, len(d)])
        rk = np.full(len(si), 999.0)
        rk[o] = (np.arange(len(o)) - np.repeat(st, cnt)) / np.repeat(cnt, cnt) * 100
        return rk

    def show(name, m, r):
        v = r[m & ~np.isnan(r)]
        if len(v):
            print("  診斷｜%-34s n=%6d 勝率 %.1f%% 平均 %+.2f%% 中位 %+.2f%%" % (name, len(v), (v > 0).mean() * 100, v.mean() * 100, np.median(v) * 100), flush=True)

    allm = np.ones(len(si), bool)
    liq = amt >= LIQ
    rk_all, rk_liq = drank(allm), drank(liq)
    show("全部低檔股 收盤→20日收盤", allm, cc)
    show("每日前3%（不管流動性）收盤→收盤", rk_all < 3, cc)
    show("每日前3%（不管流動性）隔天開盤→ 扣成本", rk_all < 3, oc)
    show("每日前3%（有流動性）收盤→收盤", rk_liq < 3, cc)
    show("每日前3%（有流動性）隔天開盤 扣成本", rk_liq < 3, oc)
    show("分數前10%（歷史百分位）收盤→收盤", spr >= 90, cc)
    show("分數前10% 隔天開盤 扣成本", spr >= 90, oc)
    show("分數前10%＋有流動性 隔天開盤 扣成本", (spr >= 90) & liq, oc)
    show("分數前5%＋有流動性 隔天開盤 扣成本", (spr >= 95) & liq, oc)
    show("分數前10% 隔天開盤跳空幅度", spr >= 90, gapo)
    for lo, hi in ((0, 0.05), (0.05, 0.2), (0.2, 1), (1, 1e9)):
        show(f"分數前10% 成交金額 {lo}～{hi if hi < 1e9 else '∞'} 億 開盤扣成本", (spr >= 90) & (amt >= lo) & (amt < hi), oc)
    for lo, hi in ((0, 15), (15, 25), (25, 40), (40, 60), (60, 101)):
        show(f"有流動性全部｜大盤站上季線 {lo}～{hi}% 開盤扣成本", liq & (br >= lo) & (br < hi), oc)
        show(f"  同上＋每日前10%", (rk_liq < 10) & (br >= lo) & (br < hi), oc)
    for y in np.unique(yrs):
        show(f"{y} 有流動性全部 開盤扣成本", liq & (yrs == y), oc)
        show(f"{y} 分數前10%＋有流動性", (spr >= 90) & liq & (yrs == y), oc)
        show(f"{y} 大盤<25%＋有流動性＋每日前10%", (rk_liq < 10) & (br < 25) & (yrs == y), oc)


def plan_backtest(O, Hh, L, C, V, si, sj, ss, dates, breadth):
    """照實際操作模擬「建議買進」：訊號當天收盤後選股 → 隔天開盤買（開太高不追）→ 碰停損賣、碰目標賣、最多抱 20 天。
    訊號用逐年檢驗的分數（每年只用之前的資料學習），報酬已扣手續費和證交稅。"""
    import numpy as np, pandas as pd
    Cv, Hv, Lv, Ov = C.values, Hh.values, L.values, O.values
    prev = C.shift(1)
    tr = pd.concat([(Hh - L).stack(), (Hh - prev).abs().stack(), (L - prev).abs().stack()], axis=1).max(axis=1).unstack().reindex(index=C.index, columns=C.columns)
    atr = tr.rolling(14).mean().values
    amt = (C * V.rolling(20).mean() / 1e5).values
    ok = (amt[si, sj] >= LIQ) & ~np.isnan(atr[si, sj])
    si, sj, ss = si[ok], sj[ok], ss[ok]
    # 每天在有流動性的低檔股裡排名（百分比，0＝最高分）
    order = np.lexsort((-ss, si))
    si, sj, ss = si[order], sj[order], ss[order]
    start = np.r_[0, np.flatnonzero(np.diff(si)) + 1]
    cnt = np.diff(np.r_[start, len(si)])
    rank = (np.arange(len(si)) - np.repeat(start, cnt)) / np.repeat(cnt, cnt) * 100
    keep = (rank < TIERS[-1][2]) | (breadth[si] < 35)     # 大盤偏弱的日子保留全部股票，給對照組用
    si, sj, rank = si[keep], sj[keep], rank[keep]
    win = si[:, None] + np.arange(1, HOLD + 1)[None, :]
    okw = win[:, -1] < len(dates)
    si, sj, rank, win = si[okw], sj[okw], rank[okw], win[okw]
    Ow, Hw, Lw, Cw = Ov[win, sj[:, None]], Hv[win, sj[:, None]], Lv[win, sj[:, None]], Cv[win, sj[:, None]]
    px, a = Cv[si, sj], atr[si, sj]
    yrs = np.array([int(dates[i][:4]) for i in si])
    br = breadth[si]                                   # 訊號當天的大盤狀態（全市場站上季線比例）
    # 最後一天沒有收盤價（停牌）就用之前最後一個有效收盤
    lastc = pd.DataFrame(Cw).ffill(axis=1).values[:, -1]

    inlist = rank < TIERS[-1][2]                         # 在推薦名單內（前 12%）

    def sim(ks, kt, sel=None, full=False):
        stop = px - ks * a
        tgt = px + kt * a if kt else np.full(len(px), np.inf)
        entry = Ow[:, 0]
        valid = ~np.isnan(entry) & (entry <= px + a) & (entry > stop) & ~np.isnan(lastc)
        hs, ht = Lw <= stop[:, None], Hw >= tgt[:, None]
        fs = np.where(hs.any(1), hs.argmax(1), 99)
        ft = np.where(ht.any(1), ht.argmax(1), 99)
        r = np.arange(len(px))
        ex_stop = np.minimum(stop, Ow[r, np.minimum(fs, HOLD - 1)])       # 跳空跌破停損就用開盤價
        ex_tgt = np.maximum(tgt, Ow[r, np.minimum(ft, HOLD - 1)])
        ex = np.where(fs <= ft, np.where(fs < 99, ex_stop, lastc), ex_tgt)   # 同一天都碰到，保守算停損
        kind = np.where(fs <= ft, np.where(fs < 99, 0, 2), 1)                # 0 停損 1 達標 2 到期
        days = np.where(kind == 0, fs, np.where(kind == 1, ft, HOLD - 1)) + 1
        ret = ex / entry - 1 - COST
        m = valid if sel is None else valid & sel
        if full:
            return ret[m], kind[m], days[m], np.flatnonzero(m)
        return ret[m], kind[m], days[m], yrs[m], si[m]

    def summ(ret, kind, days):
        if not len(ret):
            return {"n": 0}
        w, l = ret[ret > 0], ret[ret <= 0]
        return {"n": int(len(ret)), "win": round(float((ret > 0).mean() * 100), 1), "avg": round(float(ret.mean() * 100), 2),
                "med": round(float(np.median(ret) * 100), 2), "aw": round(float(w.mean() * 100), 2) if len(w) else 0, "al": round(float(l.mean() * 100), 2) if len(l) else 0,
                "stop": round(float((kind == 0).mean() * 100), 1), "tgt": round(float((kind == 1).mean() * 100), 1), "days": round(float(days.mean()), 1)}

    top = (br < STATES[1][2]) & inlist                   # 只在綠燈、黃燈的日子進場（名單內前 12%）
    grid, best = [], None
    for ks in (1.5, 2.0, 2.5, 3.0):
        for kt in (2.0, 3.0, 4.0, 6.0, 0):
            ret, kind, days, yy, _ = sim(ks, kt, top)
            st = summ(ret, kind, days)
            if not st["n"]:
                continue
            ys = [float(ret[yy == y].mean()) for y in np.unique(yy)]
            st["worst"] = round(min(ys) * 100, 2)            # 最差那一年的平均
            grid.append([ks, kt, st])
            key = st["avg"] + 0.5 * st["worst"]               # 平均要高，最差的年也不能太差
            if best is None or key > best[0]:
                best = (key, ks, kt)
    _, ks, kt = best or (0, 2.5, 4.0)   # 沒有任何可進場的日子時用預設值
    out = {"ks": ks, "kt": kt, "hold": HOLD, "cost": COST * 100, "liq": LIQ, "grid": grid, "tiers": {}, "years": [], "states": []}

    def episodes(days_idx):
        d = np.unique(days_idx)
        return int(len(d)), int(1 + (np.diff(d) > 10).sum()) if len(d) else 0   # 交易日數、波段數（相隔超過 10 天算不同波段）

    for name, lo, hi in STATES:
        ret, kind, days, yy, dd = sim(ks, kt, (br >= lo) & (br < hi) & inlist)
        st = summ(ret, kind, days)
        st["ndays"], st["nepi"] = episodes(dd)
        out["states"].append([name, lo, hi, st])
        print(f"交易計畫｜{name}（大盤 {lo}～{hi}%）：{st.get('n')} 筆、{st['ndays']} 個交易日、{st['nepi']} 個波段｜勝率 {st.get('win')}% 平均每筆 {st.get('avg')}%（賺 {st.get('aw')}%／賠 {st.get('al')}%）停損 {st.get('stop')}% 達標 {st.get('tgt')}% 抱 {st.get('days')} 天", flush=True)
    for name, lo, hi in TIERS:
        ret, kind, days, yy, _ = sim(ks, kt, top & (rank >= lo) & (rank < hi))
        out["tiers"][name] = summ(ret, kind, days)
    ret, kind, days, yy, dd = sim(ks, kt, top)
    for y in np.unique(yy):
        st = summ(ret[yy == y], kind[yy == y], days[yy == y])
        st["ndays"], st["nepi"] = episodes(dd[yy == y])
        out["years"].append([int(y), st])
        print(f"   {y}（綠燈＋黃燈）：{st['n']} 筆、{st['ndays']} 天｜勝率 {st['win']}% 平均 {st['avg']}%", flush=True)
    print(f"交易計畫回測：停損 {ks} ATR、目標 {kt or '不設'} ATR", flush=True)
    try:
        v = plan_validate(sim, summ, dict(si=si, sj=sj, rank=rank, br=br, Ow=Ow, Lw=Lw, px=px, Cv=Cv, breadth=breadth, dates=dates, ks=ks, kt=kt, inlist=inlist))
        out["states"] = v.pop("states")
        out["valid"] = v
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("建議買進驗證失敗", e, flush=True)
    return out


def portfolio_sim(trades, Cv, dates, M=10):
    """用一筆資金照計畫操作：最多同時 M 檔、每檔投入當時資金的 1/M、每天依排名先買。回傳資金曲線統計。
    trades: [(進場日索引, 出場日索引, 淨報酬, 股票欄, 排名, 進場價)]"""
    import numpy as np
    by_day = {}
    for t in trades:
        by_day.setdefault(t[0], []).append(t)
    d0 = min(by_day) if by_day else 0
    cash, pos, eq, taken, expo = 1.0, [], [], 0, 0
    for t in range(d0, len(dates)):
        val = 0.0
        for p_ in pos:
            c = Cv[t, p_["j"]]
            if c == c:
                p_["v"] = p_["alloc"] * c / p_["epx"]
            val += p_["v"]
        equity = cash + val
        held = {p_["j"] for p_ in pos}
        for tr_ in sorted(by_day.get(t, []), key=lambda x: x[4]):
            if len(pos) >= M:
                break
            alloc = equity / M
            if tr_[3] in held or cash < alloc * 0.999:
                continue
            cash -= alloc
            pos.append({"j": tr_[3], "alloc": alloc, "epx": tr_[5], "exit": tr_[1], "ret": tr_[2], "v": alloc})
            held.add(tr_[3]); taken += 1
        for p_ in [x for x in pos if x["exit"] <= t]:
            cash += p_["alloc"] * (1 + p_["ret"])
            pos.remove(p_)
        val = sum(p_["v"] for p_ in pos)
        eq.append(cash + val)
        expo += 1 if pos else 0
    eq = np.array(eq)
    if not len(eq):
        return {"n": 0}
    peak = np.maximum.accumulate(eq)
    yrs = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[d0])).days / 365.25
    ys, prev = [], 1.0
    dd = dates[d0:]
    for y in sorted({d[:4] for d in dd}):
        last = max(i for i, d in enumerate(dd) if d[:4] == y)
        ys.append([int(y), round(float((eq[last] / prev - 1) * 100), 1)])
        prev = eq[last]
    return {"n": taken, "final": round(float(eq[-1]), 2), "cagr": round(float((eq[-1] ** (1 / yrs) - 1) * 100), 1) if yrs > 0 else None,
            "mdd": round(float(((eq / peak) - 1).min() * 100), 1), "expo": round(expo / len(eq) * 100, 1), "years": ys, "from": dates[d0]}


def plan_validate(sim, summ, X):
    """強化建議買進用的大數據驗證：進場時機、對照組、每一波的成績、資金模擬"""
    import numpy as np
    si, sj, rank, br, Ow, Lw, px, Cv, b, dates, ks, kt, inlist = (X[k] for k in ("si", "sj", "rank", "br", "Ow", "Lw", "px", "Cv", "breadth", "dates", "ks", "kt", "inlist"))
    N = len(dates)
    b = np.where(np.isnan(b), 50, b)
    mn10 = np.array([b[max(0, i - 9):i + 1].min() for i in range(N)])
    ret, kind, days, ix = sim(ks, kt, None, full=True)
    S_si, S_sj, S_rank, S_br, S_in = si[ix], sj[ix], rank[ix], br[ix], inlist[ix]
    entry = Ow[ix, 0]
    mae = np.nanmin(Lw[ix], axis=1) / entry - 1                       # 持有期間最多套牢多少（概估）
    mae = np.maximum(mae, (px[ix] - ks * (px[ix] - (px[ix] - 1e-9))) * 0 + mae)

    def st(m):
        o = summ(ret[m], kind[m], days[m])
        if o["n"]:
            o["mae"] = round(float(np.mean(mae[m]) * 100), 1)
            o["ndays"] = int(len(np.unique(S_si[m])))
        return o

    out = {}
    # 0. 各燈號的成績（名單內）
    code = np.array([light_state(b[i], mn10[i]) for i in range(N)])
    out["states"] = []
    for name in LIGHTS:
        dm = code == name
        o = st(dm[S_si] & S_in)
        dd_ = np.flatnonzero(dm)
        o["ndays_all"] = int(dm.sum())
        o["nepi"] = int(1 + (np.diff(dd_) > 10).sum()) if len(dd_) else 0
        out["states"].append([name, None, None, o])
        if o["n"]:
            print("  強化0 燈號｜%-8s %4d 天 %6d 筆｜勝率 %.1f%% 平均 %+.2f%%（賺 %+.1f%%／賠 %+.1f%%）最多套牢 %.1f%%" % (name, o["ndays"], o["n"], o["win"], o["avg"], o["aw"], o["al"], o["mae"]), flush=True)
    # 1. 進場時機
    conds = {
        "綠燈（<15%）": b < 15, "黃燈（15～25%）": (b >= 15) & (b < 25), "<10%": b < 10, "<20%": b < 20,
        "綠燈且今天破底（10日最低）": (b < 15) & (b <= mn10),
        "綠燈且已回升（比10日低點高≥2）": (b < 15) & (b >= mn10 + 2),
        "止跌回升A：10日內曾<15、回升≥3、仍<30": (mn10 < 15) & (b >= mn10 + 3) & (b < 30),
        "止跌回升B：10日內曾<20、回升≥5、仍<35": (mn10 < 20) & (b >= mn10 + 5) & (b < 35),
        "止跌回升C：10日內曾<15、回升≥5、仍<35": (mn10 < 15) & (b >= mn10 + 5) & (b < 35),
        "黃燈且今天破底": (b >= 15) & (b < 25) & (b <= mn10),
        "黃燈且回升中（比10日低點高≥3）": (b >= 15) & (b < 25) & (b >= mn10 + 3),
    }
    out["timing"] = []
    for name, c in conds.items():
        o = st(c[S_si] & S_in)
        out["timing"].append([name, o])
        if o["n"]:
            print("  強化1 進場時機｜%-28s %4d 天 %6d 筆｜勝率 %.1f%% 平均 %+.2f%%（賺 %+.1f%%／賠 %+.1f%%）｜平均最多套牢 %.1f%%" % (name, o["ndays"], o["n"], o["win"], o["avg"], o["aw"], o["al"], o["mae"]), flush=True)
    # 2. 對照組：同樣的日子，名單內 vs 全部有流動性低檔股
    out["bench"] = []
    for name, lo, hi in (("綠燈", 0, 15), ("黃燈", 15, 25)):
        d = (S_br >= lo) & (S_br < hi)
        a_, t3, t12 = st(d), st(d & (S_rank < 3)), st(d & S_in)
        out["bench"].append([name, a_, t12, t3])
        if a_["n"] and t12["n"]:
            print("  強化2 對照｜%s：全部低檔股 %+.2f%%（勝率 %.1f%%）｜名單前12%% %+.2f%%（%.1f%%）｜前3%% %+.2f%%（%.1f%%）" % (name, a_["avg"], a_["win"], t12["avg"], t12["win"], t3.get("avg", 0), t3.get("win", 0)), flush=True)
    # 3. 每一波的成績（綠燈＋黃燈，名單內）
    g = (S_br < 25) & S_in
    dd = np.unique(S_si[g])
    epi, start = [], 0
    for k in range(1, len(dd) + 1):
        if k == len(dd) or dd[k] - dd[k - 1] > 10:
            seg = dd[start:k]
            m = g & (S_si >= seg[0]) & (S_si <= seg[-1])
            o = st(m)
            epi.append({"from": dates[seg[0]], "to": dates[seg[-1]], "days": int(len(seg)), "minb": round(float(b[seg].min()), 1), "n": o["n"], "win": o.get("win"), "avg": o.get("avg"), "mae": o.get("mae")})
            start = k
    out["episodes"] = epi
    for e in epi:
        print(f"  強化3 波段｜{e['from']}～{e['to']} {e['days']} 天 大盤最低 {e['minb']}%｜{e['n']} 筆 勝率 {e['win']}% 平均 {e['avg']}% 最多套牢 {e['mae']}%", flush=True)
    out["epi_win"] = [sum(1 for e in epi if (e["avg"] or 0) > 0), len(epi)]
    # 4. 資金模擬
    def trades(mask):
        idx = np.flatnonzero(mask)
        return [(int(S_si[i] + 1), int(S_si[i] + days[i]), float(ret[i]), int(S_sj[i]), float(S_rank[i]), float(entry[i])) for i in idx]
    out["port"] = {}
    isin = lambda names: np.isin(code, names)[S_si] & S_in
    for name, m in (("rec", isin(["green2", "green", "rebound"])), ("agg", isin(list(GO))), ("best", isin(["green2", "rebound"])), ("all", S_in),
                    ("綠燈＋黃燈", (S_br < 25) & S_in), ("只做綠燈", (S_br < 15) & S_in), ("止跌回升A", conds["止跌回升A：10日內曾<15、回升≥3、仍<30"][S_si] & S_in),
                    ("綠燈＋止跌回升A", (conds["綠燈（<15%）"] | conds["止跌回升A：10日內曾<15、回升≥3、仍<30"])[S_si] & S_in), ("每天都做（不看燈號）", S_in)):
        for M in (10, 5):
            r = portfolio_sim(trades(m), Cv, dates, M)
            out["port"][f"{name}/{M}"] = r
            if r.get("n"):
                print(f"  強化4 資金模擬｜{name}・最多 {M} 檔：{r['n']} 筆｜10 年變 {r['final']} 倍 年化 {r['cagr']}% 最大回檔 {r['mdd']}% 持股天數 {r['expo']}%｜各年 {r['years']}", flush=True)
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


def score_today(series, dates, fund=None, ind=None, top=150, warn=None):
    """用回測校準好的模型（20 日），幫最新一個交易日全市場低檔區股票打分數。
    回傳 {代號: 分數百分位}，並輸出 out/ai.json（分數最高的股票清單）。"""
    import bisect, json
    try:
        bt = json.load(open("cache/bt.json", encoding="utf-8"))
        m = bt.get("model")
        plan = bt.get("plan") or {}
    except Exception:
        m, plan = None, {}
    if not m:
        return {}
    last = dates[-1]
    feats = {f["k"]: f for f in m["feats"]}
    out, rows = {}, []
    # 大盤多空：今天全市場站上季線的比例
    up = tot = 0
    for c, rs in series.items():
        if len(rs) >= 60 and rs[-1][0] == last and not c.startswith("00"):
            tot += 1
            up += 1 if rs[-1][6] > sum(r[6] for r in rs[-60:]) / 60 else 0
    breadth = up / tot * 100 if tot else 50
    # 最近 120 個交易日的大盤狀態（畫趨勢用）
    bh = []
    try:
        dl = dates[-120:]
        cnt = {d: [0, 0] for d in dl}
        for c, rs in series.items():
            if c.startswith("00") or len(rs) < 200:
                continue
            cl = [r[6] for r in rs]
            run = sum(cl[-180:-120]) if len(cl) >= 180 else None
            if run is None:
                continue
            base = len(rs) - 120
            for k in range(base, len(rs)):
                run += cl[k] - cl[k - 60]
                d = rs[k][0]
                if d in cnt:
                    cnt[d][1] += 1
                    cnt[d][0] += 1 if cl[k] > run / 60 else 0
        bh = [[int(d.replace("-", "")), round(v[0] / v[1] * 100, 1)] for d, v in cnt.items() if v[1]]
    except Exception as e:
        print("大盤狀態歷史失敗", e, flush=True)
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
        if any(abs(C[i] / C[i - 1] - 1) > 0.11 for i in range(len(C) - 60, len(C))):
            continue  # 近 60 天有超過漲跌停的跳動＝資料斷層（例如未還原的減資），不評分
        if px / hi250 - 1 < -0.75 or max(C[-42:]) / min(C[-42:]) - 1 > 1.5:
            continue  # 距一年高點跌超過 75%、或 2 個月振幅超過 150%：極端崩跌股，可能有重大問題，不評分
        if px / C[-2] - 1 <= -0.095:
            continue  # 今天收跌停：賣壓還沒結束，不列入建議
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
             "gap": abs(m20 - m60) / m60 * 100,
             "r5": (px / C[-6] - 1) * 100, "amt": px * sum(V[-20:]) / 20 / 1e5, "mkt": breadth,
             "vol20": (lambda rr: (sum((x - sum(rr) / len(rr)) ** 2 for x in rr) / (len(rr) - 1)) ** 0.5 * 100)([C[i] / C[i - 1] - 1 for i in range(len(C) - 20, len(C))])}
        rv = ((fund or {}).get("s", {}).get(c, {}) or {}).get("rvy")
        if isinstance(rv, (int, float)):
            x["rvy"] = max(-100.0, min(300.0, rv))
        # ATR（14 日平均真實波幅）：給停損、目標價用
        trs = [max(Hh[i] - L[i], abs(Hh[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(len(C) - 14, len(C))]
        atr = sum(trs) / len(trs)
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
                     "v20": round(v20), "pos": round(pos, 1), "atr": round(atr, 2), "amt": round(x["amt"], 2), "lo10": round(min(L[-10:]), 2), "d60": round(x["d60"], 1), "r60": round(x["r60"], 1), "dd": round(x["dd"], 1),
                     **{k: v for k, v in ((fund or {}).get("s", {}).get(c, {})).items() if k in ("pe", "dy", "pb", "rvy", "fs", "ts")}})
    rows.sort(key=lambda r: -r["raw"])
    # ---------- 建議買進：有流動性、不是處置股的，依今天排名分級，並附交易計畫 ----------
    ks, kt = plan.get("ks", 2.0), plan.get("kt", 4.0)
    warn = warn or {}
    liquid = [r for r in rows if r["amt"] >= LIQ]
    for i, r in enumerate(liquid):
        rk = i / len(liquid) * 100
        if r["c"] in warn:
            r["warn"] = warn[r["c"]]
            continue
        for name, lo, hi in TIERS:
            if lo <= rk < hi:
                r["tier"] = name
        if "tier" in r:
            r["stop"] = round(r["px"] - ks * r["atr"], 2)
            r["tgt"] = round(r["px"] + kt * r["atr"], 2) if kt else None
            r["nc"] = round(r["px"] + r["atr"], 2)       # 不追價：隔天開盤高於這個價就不買
    mn10 = min([x[1] for x in bh[-10:]] + [breadth]) if bh else breadth
    state = light_state(breadth, mn10)
    recs = [r for r in rows if r.get("tier")]
    track = update_track(series, last, recs if state in GO else [])   # 紅燈的日子不算推薦
    keep = {r["c"] for r in rows[:top]} | {r["c"] for r in recs}
    res = {"date": last, "plan": {k: plan.get(k) for k in ("ks", "kt", "hold", "cost", "liq", "tiers", "years", "states", "valid")} if plan else None, "track": track, "state": state, "mn10": round(mn10, 1), "model_test": m.get("test"), "dec": m.get("test_dec"), "base": m.get("test_base"),
           "top5": m.get("test_top5"), "top10": m.get("test_top"), "wf": m.get("wf"), "breadth": round(breadth, 1), "bh": bh,
           "items": [r for r in rows if r["c"] in keep], "n": len(rows), "nliq": len(liquid)}
    os.makedirs("out", exist_ok=True)
    json.dump(res, open("out/ai.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"數據評分：{len(rows)} 檔（有流動性 {len(liquid)} 檔），建議買進 " + "／".join(f"{sum(1 for r in recs if r['tier'] == t[0])}" for t in TIERS) + f" 檔，處置股排除 {sum(1 for r in rows if r.get('warn'))} 檔", flush=True)
    return out


def update_track(series, last, recs, path="cache/recs.json"):
    """記錄每天的推薦，並用之後的實際走勢對答案（隔天開盤買、停損、目標、最多 20 天，扣手續費和稅）。"""
    try:
        db = json.load(open(path, encoding="utf-8"))
    except Exception:
        db = {}
    db[last] = [{"c": r["c"], "n": r["n"], "t": r["tier"], "px": r["px"], "stop": r["stop"], "tgt": r["tgt"], "nc": r["nc"]} for r in recs if r["tier"] in ("s", "b")]
    for d in sorted(db)[:-120]:
        del db[d]
    os.makedirs("cache", exist_ok=True)
    json.dump(db, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    out = []
    for d in sorted(db):
        for r in db[d]:
            rows = series.get(r["c"]) or []
            i = next((k for k, x in enumerate(rows) if x[0] == d), None)
            if i is None:
                continue
            k = rows[i][6] / r["px"] if r["px"] else 1      # 之後若有除權息，還原價會變，計畫價位跟著等比例調整
            stop, nc = r["stop"] * k, r["nc"] * k
            tgt = r["tgt"] * k if r.get("tgt") else float("inf")
            after = rows[i + 1:i + 1 + HOLD]
            e = {"d": d, "c": r["c"], "n": r["n"], "t": r["t"], "px": r["px"]}
            if not after:
                e.update(st="待進場")
            elif after[0][3] > nc or after[0][3] <= stop:
                e.update(st="未進場", why="開盤高於不追價" if after[0][3] > nc else "開盤已低於停損")
            else:
                entry, st, ex = after[0][3], None, None
                for n_, x in enumerate(after):
                    if x[5] <= stop:
                        st, ex = "停損", min(stop, x[3]); break
                    if x[4] >= tgt:
                        st, ex = "達標", max(tgt, x[3]); break
                if st is None:
                    st, ex = ("到期", after[-1][6]) if len(after) >= HOLD else ("持有中", after[-1][6])
                e.update(st=st, ret=round((ex / entry - 1 - (COST if st != "持有中" else 0)) * 100, 2), days=n_ + 1, e=round(entry / k, 2))
            out.append(e)
    done = [e for e in out if e["st"] in ("停損", "達標", "到期")]
    opn = [e for e in out if e["st"] == "持有中"]
    summ = {"n": len(done), "win": round(sum(1 for e in done if e["ret"] > 0) / len(done) * 100, 1) if done else None,
            "avg": round(sum(e["ret"] for e in done) / len(done), 2) if done else None,
            "open": len(opn), "open_avg": round(sum(e["ret"] for e in opn) / len(opn), 2) if opn else None, "since": min(db) if db else None}
    return {"sum": summ, "list": out[-120:][::-1]}


def brk_today(series, dates, fund=None, ind=None, warn=None):
    """每日「即將突破」名單：今日剛創一年新高（可進場）、接近一年新高（觀察）、近期突破的追蹤。輸出 out/brk.json"""
    try:
        bt = json.load(open("cache/bt.json", encoding="utf-8"))
        P = bt.get("brk") or {}
    except Exception:
        P = {}
    ks, hold = P.get("ks", 3.0), P.get("hold", 60)
    last, warn, fs = dates[-1], warn or {}, (fund or {}).get("s", {})
    di = {d: i for i, d in enumerate(dates)}
    LOOK = 16                                   # 追蹤最近 15 個交易日的突破
    today, recent, near = [], [], []
    up = tot = 0
    for c, rs in series.items():
        if c.startswith("00") or len(rs) < 300 or rs[-1][0] != last:
            continue
        C = [r[6] for r in rs]; Hh = [r[4] for r in rs]; L = [r[5] for r in rs]; V = [r[7] for r in rs]; O = [r[3] for r in rs]
        n = len(C)
        tot += 1
        up += 1 if C[-1] > sum(C[-60:]) / 60 else 0
        if any(abs(C[i] / C[i - 1] - 1) > 0.11 for i in range(n - 60, n)):
            continue
        amt = C[-1] * sum(V[-20:]) / 20 / 1e5
        if amt < LIQ:
            continue
        # 每一天「之前 250 天的最高價」與是否創新高（看最近 LOOK+20 天）
        span = LOOK + 21
        nh, hi_prev = {}, {}
        for k in range(n - span, n):
            if k < 251:
                continue
            hp = max(Hh[k - 250:k])
            hi_prev[k] = hp
            nh[k] = C[k] >= hp
        def fresh(k):
            return nh.get(k) and not any(nh.get(j) for j in range(k - 20, k))
        def atr_at(k):
            return sum(max(Hh[i] - L[i], abs(Hh[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(k - 13, k + 1)) / 14
        f = fs.get(c, {})
        info = {"c": c, "n": rs[-1][1], "m": rs[-1][2], "ic": (ind or {}).get(c, ""), "amt": round(amt, 2),
                **{k: v for k, v in f.items() if k in ("pe", "dy", "rvy", "fs", "ts")}}
        v20 = sum(V[-21:-1]) / 20
        for k in range(n - LOOK, n):
            if not fresh(k) or (C[k] / C[k - 1] - 1) * 100 >= 9.5:
                continue
            a = atr_at(k)
            e = dict(info, d=rs[k][0], px=round(C[k], 2), atr=round(a, 2), stop=round(C[k] - ks * a, 2), nc=round(C[k] + a, 2),
                     hi=round(hi_prev[k], 2), vr=round(V[k] / (sum(V[k - 20:k]) / 20), 1) if sum(V[k - 20:k]) else None,
                     ch=round((C[k] / C[k - 1] - 1) * 100, 2))
            if c in warn:
                e["warn"] = warn[c]
            if k == n - 1:
                today.append(e)
            else:
                # 之後的實際走勢：隔天開盤買、跌破停損賣
                if O[k + 1] > e["nc"]:
                    e.update(st="未進場", why="開盤高於不追價")
                else:
                    entry, st, ex = O[k + 1], "持有中", C[-1]
                    for j in range(k + 1, n):
                        if L[j] <= e["stop"]:
                            st, ex = "停損", min(e["stop"], O[j]); break
                    e.update(st=st, e=round(entry, 2), ret=round((ex / entry - 1 - (COST if st == "停損" else 0)) * 100, 2), days=n - 1 - k, now=round(C[-1], 2))
                recent.append(e)
        # 接近一年新高（還沒突破，且近 20 天沒創過新高）：觀察名單
        k = n - 1
        if k in hi_prev and not nh[k] and not any(nh.get(j) for j in range(k - 20, k)):
            dist = (hi_prev[k] / C[k] - 1) * 100
            if 0 < dist <= 3:
                a = atr_at(k)
                near.append(dict(info, px=round(C[k], 2), ch=round((C[k] / C[k - 1] - 1) * 100, 2), hi=round(hi_prev[k], 2), dist=round(dist, 2),
                                 atr=round(a, 2), vr=round(V[k] / v20, 1) if v20 else None, **({"warn": warn[c]} if c in warn else {})))
    breadth = up / tot * 100 if tot else 50
    today.sort(key=lambda e: -(e.get("vr") or 0))
    near.sort(key=lambda e: e["dist"])
    recent.sort(key=lambda e: (e["d"], e.get("ret") or 0), reverse=True)
    held = [e for e in recent if e["st"] in ("持有中", "停損")]
    summ = {"n": len(held), "win": round(sum(1 for e in held if e["ret"] > 0) / len(held) * 100, 1) if held else None,
            "avg": round(sum(e["ret"] for e in held) / len(held), 2) if held else None}
    res = {"date": last, "breadth": round(breadth, 1), "plan": {k: P.get(k) for k in ("sig", "hold", "ks", "trail", "cost", "liq", "all", "states", "years", "valid")} if P else None,
           "today": today, "near": near[:80], "recent": recent[:120], "rsum": summ}
    os.makedirs("out", exist_ok=True)
    json.dump(res, open("out/brk.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"即將突破：今日突破 {len(today)} 檔、接近新高 {len(near)} 檔、近 15 日突破 {len(recent)} 筆（{summ}）", flush=True)
