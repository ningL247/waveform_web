# -*- coding: utf-8 -*-
"""拿真实血流数据对比几种去噪方法, 输出频谱图和波形对比图。"""
import time, csv, numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import welch, butter, sosfiltfilt
import pywt

CSV = r"D:\DesktopD\data\data\1zhongjiawen.csv"
FS = 608.0
plt.rcParams["axes.unicode_minus"] = False

# ---- 读数据 ----
vals = []
with open(CSV) as f:
    r = csv.reader(f)
    next(r, None)
    for row in r:
        if row:
            try: vals.append(float(row[0]))
            except ValueError: pass
x = np.asarray(vals, float)
print(f"总点数 {len(x)}, 均值 {x.mean():.1f}")

# ---- 频谱(取中间一大段) ----
seg = x[100000:160000]
f, p = welch(seg - seg.mean(), fs=FS, nperseg=8192)
band = (f >= 0.3) & (f <= 20)
fb, pb = f[band], p[band]
peak = fb[np.argmax(pb)]
print(f"0.3-20Hz 内主峰频率 ≈ {peak:.2f} Hz (≈ {peak*60:.0f} 次/分, 像心率)")

plt.figure(figsize=(8, 4))
plt.semilogy(f[f <= 30], p[f <= 30])
plt.axvline(peak, color="r", ls="--", label=f"主峰 {peak:.2f} Hz")
plt.xlabel("频率 Hz"); plt.ylabel("功率谱密度"); plt.legend(); plt.title("血流信号频谱")
plt.tight_layout(); plt.savefig("analysis_psd.png", dpi=110)
print("已保存 analysis_psd.png")

# ---- 取一窗做方法对比 ----
N = 4096
w = x[120000:120000 + N].copy()
t = np.arange(N) / FS

def center(y): return y - np.mean(y)

# 1) 带通 0.5-8 (零相位, 离线公平对比)
sos = butter(4, [0.5, 8], btype="band", fs=FS, output="sos")
y_bp = sosfiltfilt(sos, w)

# 2) 小波去噪 (sym8, 软阈值 + 去基线)
wv = "sym8"; lvl = 6
coeffs = pywt.wavedec(w, wv, level=lvl)
sigma = np.median(np.abs(coeffs[-1])) / 0.6745
thr = sigma * np.sqrt(2 * np.log(N))
coeffs[0] = np.zeros_like(coeffs[0])                       # 去基线/趋势
coeffs[1:] = [pywt.threshold(c, thr, "soft") for c in coeffs[1:]]
y_wt = center(pywt.waverec(coeffs, wv)[:N])

# 3) EEMD 重构 (保留主峰所在频带的 IMF)
from PyEMD import EEMD
t0 = time.time()
eemd = EEMD(trials=40); eemd.noise_seed(0)
imfs = eemd.eemd(w, max_imf=8)
def dom_freq(s):
    sp = np.abs(np.fft.rfft(s - s.mean())); fr = np.fft.rfftfreq(len(s), 1/FS)
    return fr[np.argmax(sp)]
keep = [i for i, im in enumerate(imfs) if 0.5 <= dom_freq(im) <= 8]
y_eemd = center(imfs[keep].sum(0)) if keep else np.zeros(N)
print(f"EEMD: {len(imfs)} 个IMF, 保留频带内 {keep}, 用时 {time.time()-t0:.1f}s")

# 4) VMD 重构 (中心频率在脉动带内的模态)
from vmdpy import VMD
u, _, omega = VMD(w, alpha=2000, tau=0, K=6, DC=0, init=1, tol=1e-6)
cf = omega[-1] * FS
keepv = [i for i in range(len(u)) if 0.4 <= cf[i] <= 8]
y_vmd = center(u[keepv].sum(0)) if keepv else np.zeros(N)
print(f"VMD: 各模态中心频率 {np.round(cf,2)}, 保留 {keepv}")

# ---- 噪声指标: 二阶差分能量(越小越平滑/越不噪) ----
def rough(y): return np.sqrt(np.mean(np.diff(y, 2) ** 2))
methods = [("原始(去均值)", center(w)), ("带通0.5-8", center(y_bp)),
           ("小波去噪", y_wt), ("EEMD重构", y_eemd), ("VMD重构", y_vmd)]
print("\n粗糙度(越小越干净):")
for name, y in methods: print(f"  {name:12s} {rough(y):.4f}")

# ---- 波形对比图 ----
fig, axes = plt.subplots(len(methods), 1, figsize=(11, 11), sharex=True)
for ax, (name, y) in zip(axes, methods):
    ax.plot(t, y, lw=0.8)
    ax.set_ylabel(name, fontsize=10); ax.grid(alpha=0.3)
axes[-1].set_xlabel("时间 s")
fig.suptitle(f"血流脉动去噪方法对比 (fs={FS:.0f}Hz, 主峰≈{peak:.2f}Hz)")
plt.tight_layout(); plt.savefig("analysis_compare.png", dpi=110)
print("已保存 analysis_compare.png")
