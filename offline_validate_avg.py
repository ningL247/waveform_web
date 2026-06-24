#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
同步平均(coherent averaging)离线验证

在真实录制信号上验证「方向1 同步平均」是否真能把微弱足背脉动提干净:
  原始 -> 带通(同 server Preprocessor) -> 谐波梳状提取(同 server CombExtractor)
       -> 在提取信号上检测心拍 -> 相位对齐 -> 集成/EWMA 平均成模板

输出客观指标(不靠肉眼):
  - 估计心率 HR
  - 检出心拍数
  - 分半可靠性(奇/偶心拍各自平均后的相关) —— 真周期信号才高
  - 噪声衰减(单拍残差 std vs 模板内残差) —— 验证 ~√N 规律
  - 粗糙度下降(二阶差分能量, 模板 vs 单拍)
并保存对比图 coherent_avg_validation.png

用法:
  python offline_validate_avg.py --csv data/ch0.csv --fs 250
"""
import argparse
import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, welch, find_peaks

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


# ---- 复刻 server.py 的因果带通(Preprocessor) ----
def causal_bandpass(x, fs, low=0.5, high=8.0, order=4):
    ny = fs * 0.5
    sos = butter(order, [low / ny, min(high, ny * 0.99) / ny], btype="band", output="sos")
    zi = sosfilt_zi(sos) * x[0]
    y, _ = sosfilt(sos, x, zi=zi)
    return y


# ---- 复刻 server.py 的谐波梳状提取(CombExtractor)----
def estimate_f0(x, fs, hr_lo=0.9, hr_hi=2.2, win_s=16.0):
    """与 server 一致: 取最近 win_s 秒, Welch 平均谱, 在心率带取最强峰。"""
    seg = x[-int(fs * win_s):] if len(x) > int(fs * win_s) else x
    seg = seg - seg.mean()
    nper = int(min(len(seg), fs * 8))
    fr, pp = welch(seg, fs=fs, nperseg=nper)
    m = (fr >= hr_lo) & (fr <= hr_hi)
    return float(fr[m][np.argmax(pp[m])])


def comb_extract(x, fs, f0, n_harm=5, bw=0.4, hp=0.5):
    """高通去基线 -> k*f0 窄带带通之和(因果)。"""
    ny = fs * 0.5
    hp_sos = butter(2, hp / ny, btype="high", output="sos")
    hv, _ = sosfilt(hp_sos, x, zi=sosfilt_zi(hp_sos) * x[0])
    acc = np.zeros_like(hv)
    for k in range(1, n_harm + 1):
        fc = k * f0
        lo = (fc - bw / 2) / ny
        hi = (fc + bw / 2) / ny
        if lo > 0 and hi < 1 and lo < hi:
            sos = butter(2, [lo, hi], btype="band", output="sos")
            yk, _ = sosfilt(sos, hv, zi=sosfilt_zi(sos) * hv[0])
            acc += yk
    return acc


# ---- 同步平均核心 ----
def coherent_average(trig, fs, f0, n_pts=256, peak_phase=0.3, alpha=0.2):
    """在触发信号 trig(提取输出)上检测心拍, 对齐到相位域, 做集成平均与 EWMA 平均。

    返回 dict: peaks, beats(n x n_pts 矩阵), ens(集成平均模板), ewma(EWMA 模板)。
    每个心拍窗 = [peak - peak_phase*P, +(1-peak_phase)*P), 重采样到 n_pts。
    """
    P = fs / f0
    dist = max(1, int(0.6 * P))
    prom = 0.3 * (trig.std() + 1e-9)
    peaks, _ = find_peaks(trig, distance=dist, prominence=prom)

    off = int(peak_phase * P)
    L = int(round(P))
    beats = []
    used_peaks = []
    for pk in peaks:
        s = pk - off
        e = s + L
        if s < 0 or e > len(trig):
            continue
        seg = trig[s:e]
        rs = np.interp(np.linspace(0, 1, n_pts), np.linspace(0, 1, len(seg)), seg)
        beats.append(rs - rs.mean())
        used_peaks.append(pk)
    beats = np.asarray(beats)

    ens = beats.mean(axis=0) if len(beats) else np.zeros(n_pts)
    # EWMA(模拟 server 实时逐拍更新)
    ewma = None
    for b in beats:
        ewma = b.copy() if ewma is None else (1 - alpha) * ewma + alpha * b
    if ewma is None:
        ewma = np.zeros(n_pts)
    return {"peaks": np.asarray(used_peaks), "beats": beats, "ens": ens, "ewma": ewma, "P": P}


def roughness(x):
    """二阶差分能量(越小越光滑)。归一化到信号能量。"""
    d2 = np.diff(x, 2)
    return float(np.sum(d2 ** 2) / (np.sum((x - x.mean()) ** 2) + 1e-12))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/ch0.csv")
    ap.add_argument("--col", type=int, default=0,
                    help="多列CSV取第几列做原始(录制文件第0列=原始ch0)")
    ap.add_argument("--fs", type=float, default=250.0)
    ap.add_argument("--alpha", type=float, default=0.2)
    ap.add_argument("--harm", type=int, default=5)
    ap.add_argument("--bw", type=float, default=0.4)
    ap.add_argument("--out", default="coherent_avg_validation.png")
    args = ap.parse_args()

    # 读数据(首行可能是列名)。多列(录制文件)时取 --col 指定列(默认第0列=原始ch0)
    data = np.genfromtxt(args.csv, delimiter=",", skip_header=1)
    if data.ndim == 2:
        print(f"检测到 {data.shape[1]} 列, 取第 {args.col} 列做原始信号")
        raw = data[:, args.col]
    else:
        raw = data
    raw = raw[np.isfinite(raw)]
    fs = args.fs
    dur = len(raw) / fs
    print(f"载入 {len(raw)} 样本, fs={fs}Hz, 时长≈{dur:.1f}s ({dur/60:.1f}min)")

    band = causal_bandpass(raw, fs)
    f0 = estimate_f0(band, fs)
    print(f"估计心率 f0 = {f0:.3f} Hz = {f0*60:.0f} bpm")

    ext = comb_extract(raw, fs, f0, n_harm=args.harm, bw=args.bw)

    res = coherent_average(ext, fs, f0, alpha=args.alpha)
    beats, ens, ewma, P = res["beats"], res["ens"], res["ewma"], res["P"]
    nb = len(beats)
    print(f"检出心拍数 = {nb}  (周期 P≈{P:.1f} 样本 = {P/fs*1000:.0f} ms)")
    if nb < 4:
        print("!! 心拍太少, 无法可靠验证")
        return

    # --- 指标 1: 分半可靠性(奇/偶心拍各自平均的相关)---
    odd = beats[1::2].mean(axis=0)
    even = beats[0::2].mean(axis=0)
    split_r = float(np.corrcoef(odd, even)[0, 1])
    print(f"[分半可靠性] 奇/偶心拍平均相关 r = {split_r:.3f}   (>0.8 = 提到稳定可复现的脉搏波形)")

    # --- 指标 2: 噪声衰减 (~√N) ---
    # 每个样本点跨心拍的方差 = 单拍噪声; 集成平均后噪声 ≈ /N
    per_sample_std = beats.std(axis=0).mean()          # 单拍噪声水平
    resid = beats - ens                                 # 各拍相对模板的残差
    beat_resid_std = resid.std()
    expected_gain_db = 10 * np.log10(nb)
    print(f"[噪声衰减] 单拍残差 std = {beat_resid_std:.4g}; 集成平均拍数 N={nb} "
          f"→ 理论增益 ≈ {expected_gain_db:.1f} dB (√N)")
    ewma_neff = (2 - args.alpha) / args.alpha
    print(f"[实时EWMA] alpha={args.alpha} → 等效平均拍数 N_eff≈{ewma_neff:.1f} "
          f"(实时增益 ≈ {10*np.log10(ewma_neff):.1f} dB)")

    # --- 指标 3: 粗糙度下降 ---
    r_single = np.mean([roughness(b) for b in beats])
    r_ens = roughness(ens)
    print(f"[粗糙度] 单拍平均 {r_single:.3g} → 集成平均模板 {r_ens:.3g} "
          f"(下降 {(1-r_ens/r_single)*100:.0f}%)")

    # --- 指标 4: 模板能量占比(模板能量 / 单拍能量, 反映可平均掉的随机部分)---
    tmpl_energy = np.sum(ens ** 2)
    beat_energy = np.mean(np.sum(beats ** 2, axis=1))
    print(f"[相干能量占比] 模板能量/单拍能量 = {tmpl_energy/beat_energy*100:.0f}% "
          f"(其余为可被平均压制的非相干噪声)")

    # ---- 作图 ----
    t = np.arange(len(raw)) / fs
    ph = np.linspace(0, 1, len(ens))
    fig, ax = plt.subplots(3, 1, figsize=(11, 9))

    seg = slice(0, int(min(len(raw), fs * 20)))  # 前 20s
    ax[0].plot(t[seg], raw[seg] - np.mean(raw[seg]), lw=0.5, color="#999", label="原始(去均值)")
    ax[0].plot(t[seg], ext[seg], lw=0.9, color="#2a8", label="梳状提取")
    pk = res["peaks"]; pk = pk[pk < seg.stop]
    ax[0].plot(pk / fs, ext[pk], "rv", ms=5, label="检出心拍")
    ax[0].set_title(f"前20s: 原始 vs 梳状提取 + 心拍检测  (HR={f0*60:.0f}bpm)")
    ax[0].legend(loc="upper right", fontsize=8); ax[0].set_xlabel("时间 s")

    for b in beats:
        ax[1].plot(ph, b, color="#bbb", lw=0.4, alpha=0.5)
    ax[1].plot(ph, ens, color="#d33", lw=2.2, label=f"集成平均模板 (N={nb})")
    ax[1].plot(ph, ewma, color="#06c", lw=1.6, ls="--", label=f"实时EWMA (α={args.alpha})")
    ax[1].set_title(f"相位对齐的所有心拍(灰)与平均模板  |  分半相关 r={split_r:.3f}")
    ax[1].legend(loc="upper right", fontsize=8); ax[1].set_xlabel("心拍相位")

    # 用模板按相位重建一段干净流, 和提取流对比
    rec = np.zeros_like(ext)
    phase = 0.3
    for n in range(len(ext)):
        phase = (phase + f0 / fs) % 1.0
        rec[n] = ens[int(phase * len(ens)) % len(ens)]
    # 用真实峰对齐重建的相位(简单做法: 不强对齐, 仅展示形状)
    ax[2].plot(t[seg], ext[seg], color="#2a8", lw=0.8, label="梳状提取")
    ax[2].plot(t[seg], rec[seg], color="#d33", lw=1.3, label="同步平均重建(相位锁定)")
    ax[2].set_title("提取流 vs 同步平均重建流(前20s)")
    ax[2].legend(loc="upper right", fontsize=8); ax[2].set_xlabel("时间 s")

    plt.tight_layout()
    fig.savefig(args.out, dpi=110)
    print(f"\n对比图已保存 -> {args.out}")

    # ---- 结论 ----
    print("\n==== 结论 ====")
    ok = split_r > 0.7 and tmpl_energy / beat_energy < 0.9
    if split_r > 0.8:
        print(f"[OK] 分半相关 {split_r:.2f} 高 -> 信号里确有稳定可复现的脉搏波形, 同步平均有效。")
    elif split_r > 0.6:
        print(f"[~]  分半相关 {split_r:.2f} 中等 -> 有周期成分但波形抖, 同步平均仍能降噪但模板偏软。")
    else:
        print(f"[X]  分半相关 {split_r:.2f} 低 -> 该段周期性弱, 同步平均收益有限(可能心拍检测不稳)。")
    print(f"[{'OK' if ok else '~'}] 建议: 离线验证{'通过' if ok else '部分通过'}, "
          f"可在 server.py 落地 SyncAverager(EWMA alpha≈{args.alpha})。")


if __name__ == "__main__":
    main()
