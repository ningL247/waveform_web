#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
波形上位机 —— 蓝牙串口 -> WebSocket 桥接服务器

两种模式:
  1) 真实模式: 从 JDY-31 蓝牙串口(配对后的 COM 口)读取 VOFA+ FireWater 文本帧
       python server.py --serial COM5 --baud 115200
  2) 演示模式: 回放一个 CSV 文件(不接硬件也能看波形)
       python server.py --demo "D:\DesktopD\data\data\1zhongjiawen.csv" --rate 300

服务器同时:
  - 用 HTTP 提供网页 (static/index.html)
  - 用 WebSocket (/ws) 把解析出的样本批量推给浏览器

手机/电脑只要和本机在同一 WiFi 下, 打开 http://<本机IP>:8080 即可看波形。
"""

import argparse
import asyncio
import csv
import datetime
import json
import threading
import time
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, welch, find_peaks
from aiohttp import web

import sys as _sys
_BASE = Path(_sys._MEIPASS) if getattr(_sys, "frozen", False) else Path(__file__).parent
STATIC_DIR = _BASE / "static"


# ---------------------------------------------------------------------------
# 采样率跟踪(原 Preprocessor 去掉滤波后保留的 fs 测量/存储部分)
# ---------------------------------------------------------------------------
class FsTracker:
    def __init__(self, fs=None):
        self.fs = fs
        self.lock = threading.Lock()
        self._t0 = None
        self._count = 0
        self._locked = fs is not None

    def set_fs(self, fs):
        with self.lock:
            self.fs = float(fs); self._locked = True

    def measure(self):
        """每来一帧调用一次, 2s 后自动锁定 fs。"""
        if self._locked:
            return
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now; self._count = 0; return
        self._count += 1
        if now - self._t0 >= 2.0:
            with self.lock:
                self.fs = self._count / (now - self._t0)
                self._locked = True
            print(f"[fs] 自动测得采样率 ≈ {self.fs:.1f} Hz")

    def status(self):
        return {"type": "fs", "fs": round(self.fs, 1) if self.fs else None}


preproc = FsTracker()


# ---------------------------------------------------------------------------
# 实时谐波梳状提取: 去基线 -> 跟踪心率 f0 -> 只保留 k*f0 窄带之和
# 用于把微弱周期脉动从宽带噪声中提取出来(弱信号检测)。
# ---------------------------------------------------------------------------
class CombExtractor:
    _F0_HIST_LEN = 8      # 最近 N 次 f0 估计(每次 ~0.5s, 共约 4s)
    # 置信度分级阈值(f0 历史 std, Hz):
    #   < STD_OK  → 2(可用)   < STD_LOW → 1(低置信)   否则 → 0(极低)
    _STD_OK  = 0.20
    _STD_LOW = 0.35
    # 时间连续性: 偏离上次 f0 的代价权重(归一化Q分/Hz), 按置信度分档
    _CONT_W  = {0: 0.3, 1: 0.8, 2: 1.5}
    # 重捕获: 原始最优持续偏离当前轨道超过此频率(Hz)且累计 RECAP_N 帧则强制跳轨
    _RECAP_DF = 0.20
    _RECAP_N  = 6
    # 自适应齿宽: conf=2(稳定)时收窄到 ±0.12 Hz, 其余保持宽带
    _BW_NARROW = 0.24   # 稳定时齿宽(Hz) = ±0.12 Hz 半宽
    _BW_WIDE   = 0.4    # 低置信/冷启动时齿宽(Hz)

    def __init__(self, fs=None, enabled=True, n_harm=5, bw=0.4, hp=0.5,
                 hr_lo=0.7, hr_hi=2.5):
        self.fs = fs
        self.enabled = enabled
        self.n_harm = n_harm
        self.bw = bw          # 用户配置的默认宽带宽(低置信时使用)
        self._bw_active = bw  # 当前生效齿宽(自适应: conf=2 时切换为 _BW_NARROW)
        self.hp = hp
        self.hr_lo, self.hr_hi = hr_lo, hr_hi
        self.f0 = None                # 跟踪到的心率基频(Hz)
        self.f0_confidence = 0        # 0=极低 / 1=低置信 / 2=可用
        self._f0_history = []         # 最近 N 次 f0 估计(用于稳定性分级)
        self._challenge = 0           # 重捕获计数: 原始最优持续偏离当前轨道的帧数
        self._q_score   = 0.0         # 最新帧 HPS 谐波得分(信号强度指标)
        self._f0_std    = 0.0         # 最近 f0 历史标准差(跟踪稳定性)
        self.lock = threading.Lock()
        self.hp_sos = None; self.hp_zi = {}
        self.comb = []; self.comb_zi = {}
        self.buf = {}
        self._since = 0
        if fs:
            self._build_hp()

    def _build_hp(self):
        if not self.fs or self.fs <= 0:
            self.hp_sos = None; return
        ny = self.fs * 0.5
        self.hp_sos = butter(2, min(max(self.hp, 1e-3) / ny, 0.99),
                             btype="high", output="sos")
        self.hp_zi = {}

    def _build_comb(self):
        self.comb = []; self.comb_zi = {}
        if not self.fs or not self.f0:
            return
        ny = self.fs * 0.5
        bw = self._bw_active
        for k in range(1, self.n_harm + 1):
            fc = k * self.f0
            lo = (fc - bw / 2) / ny; hi = (fc + bw / 2) / ny
            if lo > 0 and hi < 1 and lo < hi:
                self.comb.append(butter(2, [lo, hi], btype="band", output="sos"))

    def update(self, enabled=None, n_harm=None, bw=None, fs=None):
        with self.lock:
            if enabled is not None: self.enabled = bool(enabled)
            if n_harm is not None: self.n_harm = max(1, min(8, int(n_harm)))
            if bw is not None: self.bw = max(0.1, float(bw))
            if fs is not None: self.fs = float(fs)
            # bw 更新时: 非稳定状态跟随用户设置, 稳定状态保持窄带
            if self.f0_confidence < 2:
                self._bw_active = self.bw
            self._build_hp(); self._build_comb()

    def status(self):
        return {"type": "extract", "enabled": self.enabled, "n_harm": self.n_harm,
                "bw": self.bw, "bw_active": round(self._bw_active, 3),
                "f0": round(self.f0, 3) if self.f0 else None,
                "hr": round(self.f0 * 60) if self.f0 else None,
                "confidence": self.f0_confidence,   # 0/1/2
                "q": round(self._q_score, 1),
                "f0_std": round(self._f0_std, 3)}

    def _estimate(self):
        b = self.buf.get(0)
        if not b or not self.fs:
            return
        x = np.asarray(b, dtype=float)
        if len(x) < int(self.fs * 8):
            return
        x = x - x.mean()
        nper = int(min(len(x), self.fs * 8))
        fr, pp = welch(x, fs=self.fs, nperseg=nper)
        m = (fr >= self.hr_lo) & (fr <= self.hr_hi)
        if not m.any():
            return

        # ---- 谐波加权评分替代 argmax ----
        # 三项改进:
        #   1. 窗口收窄 ±0.15→±0.10 Hz: 防止相邻强峰被误算为谐波
        #   2. 局部噪声底: 每个谐波位置单独估计, 比全局 median 更准
        #   3. 邻近度权重: 峰离 k*f 越远贡献越小, 防止偏移峰混入
        HW = 0.10   # harmonic window half-width (Hz)
        fr_m = fr[m]
        global_noise = float(np.median(pp[m]))
        weights = [(1, 1.0), (2, 0.5), (3, 0.25)]
        # 先算出所有候选的 HPS 得分
        cand_q = np.zeros(len(fr_m))
        for ci, f_cand in enumerate(fr_m):
            q = 0.0
            for k, w in weights:
                fc = k * f_cand
                mk = (fr >= fc - HW) & (fr <= fc + HW)
                if not mk.any():
                    continue
                peak_pow  = float(pp[mk].max())
                peak_freq = float(fr[mk][np.argmax(pp[mk])])
                local_m = (fr >= fc - 0.5) & (fr <= fc + 0.5) & ~mk
                local_noise = float(np.median(pp[local_m])) if local_m.any() \
                              else global_noise
                local_noise = max(local_noise, global_noise * 0.5, 1e-12)
                proximity = max(0.0, 1.0 - abs(peak_freq - fc) / HW)
                q += w * (peak_pow / local_noise) * proximity
            cand_q[ci] = q

        # ---- 时间连续性惩罚 + 重捕获 ----
        # 不带惩罚的原始最优(用于重捕获判定)
        raw_f = float(fr_m[int(np.argmax(cand_q))])
        qmax = float(cand_q.max()) + 1e-12
        prev = self.f0
        if prev is None:
            w_cont = 0.0          # 冷启动: 自由捕获
        else:
            w_cont = self._CONT_W.get(self.f0_confidence, 0.8)
        # Q 归一化后减去偏离上次 f0 的代价
        best_i, best_s = int(np.argmax(cand_q)), -1e9
        for ci, f_cand in enumerate(fr_m):
            s = cand_q[ci] / qmax
            if prev is not None:
                s -= w_cont * abs(float(f_cand) - prev)
            if s > best_s:
                best_s, best_i = s, ci
        best_f = float(fr_m[best_i])

        # 重捕获逃生阀: 原始最优持续偏离当前轨道 -> 强制跳轨, 防止锁死在错误频率
        if prev is not None and abs(raw_f - prev) > self._RECAP_DF:
            self._challenge += 1
        else:
            self._challenge = 0
        if self._challenge >= self._RECAP_N:
            best_f = raw_f
            best_i = int(np.argmax(cand_q))
            self._challenge = 0
            self._f0_history = []     # 清空历史, 重新评估置信度
            print(f"[extract] f0 重捕获 -> {raw_f:.3f}Hz ({raw_f*60:.0f}bpm)")

        # 抛物线插值微调, 减少频率分辨率量化跳动
        idx = int(np.where(fr == best_f)[0][0])
        if 0 < idx < len(fr) - 1:
            y0, y1, y2 = pp[idx-1], pp[idx], pp[idx+1]
            denom = y0 - 2*y1 + y2
            if abs(denom) > 1e-12:
                delta = 0.5 * (y0 - y2) / denom
                best_f = float(best_f + delta * (fr[1] - fr[0]))
        f0 = best_f

        # ---- f0 置信度分级(基于历史稳定性) ----
        self._f0_history.append(f0)
        if len(self._f0_history) > self._F0_HIST_LEN:
            del self._f0_history[0]
        conf = 0
        if len(self._f0_history) >= 4:
            std = float(np.std(self._f0_history))
            if std < self._STD_OK:
                conf = 2   # 可用
            elif std < self._STD_LOW:
                conf = 1   # 低置信, 仍显示但标记
            # else: conf=0, 极低

        # 保存 Q 分和 f0_std 供前端显示
        f0_std_val = float(np.std(self._f0_history)) if len(self._f0_history) >= 2 else 0.0
        self._q_score = float(cand_q[best_i])
        self._f0_std  = f0_std_val

        # 自适应齿宽: 置信度升到 2 时收窄, 降低则恢复宽带
        bw_target = self._BW_NARROW if conf == 2 else self.bw
        with self.lock:
            bw_changed = (bw_target != self._bw_active)
            f0_changed  = (self.f0 is None or abs(f0 - self.f0) > 0.03)
            if f0_changed or bw_changed:
                if bw_changed:
                    print(f"[extract] bw 自适应: {self._bw_active:.2f}→{bw_target:.2f}Hz "
                          f"(conf {self.f0_confidence}→{conf})")
                self.f0 = f0
                self._bw_active = bw_target
                self._build_comb()
            self.f0_confidence = conf

    def process(self, values):
        if self.fs is None and preproc.fs:
            with self.lock:
                self.fs = preproc.fs
                self._build_hp()
        out = [0.0] * len(values)
        with self.lock:
            if self.enabled and self.hp_sos is not None:
                for i, v in enumerate(values):
                    if i not in self.hp_zi:
                        self.hp_zi[i] = sosfilt_zi(self.hp_sos) * v
                    y, self.hp_zi[i] = sosfilt(self.hp_sos, [v], zi=self.hp_zi[i])
                    hv = float(y[0])
                    bb = self.buf.setdefault(i, [])
                    bb.append(hv)
                    cap = int(self.fs * 16)
                    if len(bb) > cap:
                        del bb[:len(bb) - cap]
                    # 不管置信度高低, 梳状滤波始终输出(置信度只影响颜色/透明度)
                    if self.comb:
                        if i not in self.comb_zi:
                            self.comb_zi[i] = [sosfilt_zi(s) * hv for s in self.comb]
                        acc = 0.0
                        for j, s in enumerate(self.comb):
                            yk, self.comb_zi[i][j] = sosfilt(s, [hv], zi=self.comb_zi[i][j])
                            acc += float(yk[0])
                        out[i] = acc
        self._since += len(values)
        if self.fs and self._since >= int(self.fs * 0.5):
            self._since = 0
            self._estimate()
        return out


comb = CombExtractor()


# ---------------------------------------------------------------------------
# 实时同步平均(coherent / ensemble averaging): 方向1
# 在「提取」输出上检测心拍 -> 相位对齐 -> 在相位域维护 EWMA 平均模板
# -> 按心率相位锁定重建一条干净「平均脉搏波」。噪声按 ~√N 衰减。
# 模板存在相位域(0..1 一个心拍), 故对心率漂移(HRV)天然鲁棒。
# 离线验证: data/ch0.csv 上 561 拍, 分半相关 r=0.998, 实时增益≈9.5dB(α=0.2)。
# ---------------------------------------------------------------------------
class SyncAverager:
    # 冷启动缓冲池大小: 最多缓存这么多候选拍, 再从中选共识拍建初始模板
    BOOTSTRAP_N   = 8
    # 共识判定阈值: 候选拍与其他拍的平均相关 >= 此值才算"可信拍"
    BOOTSTRAP_R   = 0.25
    # 自动重置: 滚动窗口大小(拍数), 拒绝率 > RESET_THRESH 时清空模板重来
    RESET_WIN     = 10
    RESET_THRESH  = 0.70

    def __init__(self, fs=None, enabled=True, alpha=0.2, n_pts=256,
                 min_beats=3, peak_phase=0.3, gate_r=0.2):
        self.fs = fs
        self.enabled = enabled
        self.alpha = alpha            # EWMA 系数
        self.n_pts = n_pts            # 模板相位分辨率
        self.min_beats = min_beats    # 输出前至少平均的拍数
        self.peak_phase = peak_phase  # 收缩峰在模板中的相位
        self.gate_r = gate_r          # 门控相关系数阈值
        self.lock = threading.Lock()
        self.buf = {}                 # 每通道触发信号滚动缓冲
        self.abs0 = {}                # buf[0] 对应的绝对样本号
        self.count = {}               # 已处理绝对样本数
        self.template = {}            # 每通道 EWMA 模板
        self.nbeats = {}              # 已纳入模板的拍数
        self.nrejected = {}           # 被门控拒绝的拍数(自上次重置)
        self.nresets = {}             # 自动重置次数
        self.phase = {}               # 当前播放相位 [0,1)
        self.dphase_extra = {}        # PLL 渐进对齐每样本附加相位
        self.last_pk_abs = {}         # 最近已处理峰的绝对样本号
        # 冷启动: 每通道候选拍缓冲池(list of np.array)
        self.boot_pool = {}
        # 自动重置: 滚动窗口记录最近 RESET_WIN 次门控结果(True=接受, False=拒绝)
        self.gate_history = {}
        self._since = 0
        # 启动等待期: 跳过开头 N 秒，等 ADC/高通滤波器基线稳定后再收集心拍
        # 每次手动重置也会重新触发等待(防止重置时的接触伪迹污染模板)
        self.startup_delay = 5.0   # 秒，可通过 update() 调整
        self._startup_samples = 0  # 等待期内已收到的样本数(达到 fs*delay 后解锁)

    def update(self, enabled=None, alpha=None, fs=None, gate_r=None, startup_delay=None):
        with self.lock:
            if enabled is not None: self.enabled = bool(enabled)
            if alpha is not None: self.alpha = min(1.0, max(0.01, float(alpha)))
            if fs is not None: self.fs = float(fs)
            if gate_r is not None: self.gate_r = min(1.0, max(0.0, float(gate_r)))
            if startup_delay is not None: self.startup_delay = max(0.0, float(startup_delay))

    def status(self):
        nb   = int(self.nbeats.get(0, 0))
        nrj  = int(self.nrejected.get(0, 0))
        nrst = int(self.nresets.get(0, 0))
        pool = len(self.boot_pool.get(0, []))
        bootstrapping = self.template.get(0) is None
        fs   = self.fs or comb.fs or preproc.fs or 250.0
        startup_thresh = int(fs * self.startup_delay)
        startup_remaining = max(0.0, (startup_thresh - self._startup_samples) / fs)
        return {"type": "average", "enabled": self.enabled,
                "alpha": round(self.alpha, 3), "gate_r": round(self.gate_r, 2),
                "beats": nb, "rejected": nrj, "resets": nrst,
                "bootstrapping": bootstrapping, "boot_pool": pool,
                "ready": nb >= self.min_beats,
                "startup": round(startup_remaining, 1) if startup_remaining > 0 else 0}

    def _corr(self, a, b):
        """两段等长信号的 Pearson 相关系数。"""
        denom = a.std() * b.std() + 1e-12
        return float(np.dot(a - a.mean(), b - b.mean()) / (len(a) * denom))

    def _reset_template(self, i):
        """清空通道 i 的模板, 重新走冷启动流程(保留相位估计)。"""
        self.template.pop(i, None)
        self.nbeats[i] = 0
        self.nrejected[i] = 0
        self.nresets[i] = self.nresets.get(i, 0) + 1
        self.boot_pool[i] = []
        self.gate_history[i] = []
        self.last_pk_abs.pop(i, None)
        # 重置等待期计数器, 让接触调整后的瞬态也被跳过
        self._startup_samples = 0

    def reset(self):
        """手动重置所有通道模板, 重新冷启动(线程安全)。"""
        with self.lock:
            for i in list(self.buf.keys()) or [0]:
                self._reset_template(i)
        print("[avg] 手动重置模板, 重新冷启动")

    def _rebuild(self, fs, P):
        """周期性: 在缓冲上检测心拍, 把新拍 EWMA 进模板, 并把播放相位对齐到最近真实峰。"""
        with self.lock:
            chans = list(self.buf.keys())
        off = int(self.peak_phase * P)
        L = int(round(P))
        N = self.n_pts
        for i in chans:
            with self.lock:
                b = np.asarray(self.buf.get(i, []), dtype=float)
                abs0 = self.abs0.get(i, 0)
                cur_abs = self.count.get(i, 0)
            if len(b) < int(P * 2.5):
                continue
            x = b - b.mean()
            dist = max(1, int(0.6 * P))
            prom = 0.3 * (x.std() + 1e-9)
            peaks, _ = find_peaks(x, distance=dist, prominence=prom)
            if len(peaks) < 2:
                continue
            with self.lock:
                last_inc = self.last_pk_abs.get(i, -1)
            for pk in peaks:
                pk_abs = abs0 + int(pk)
                if pk_abs <= last_inc:
                    continue            # 已纳入过
                s = pk - off
                e = s + L
                if s < 0 or e > len(b):
                    continue            # 心拍窗不完整, 下轮再纳入
                seg = b[s:e]
                rs = np.interp(np.linspace(0, 1, N),
                               np.linspace(0, 1, len(seg)), seg)
                # 去线性趋势使首尾相等 -> 相位环 1->0 处连续, 消除每拍跳变
                rs = rs - np.linspace(rs[0], rs[-1], N)
                with self.lock:
                    tpl = self.template.get(i)
                    if tpl is None:
                        # ---- 冷启动: 共识选拍 ----
                        # 把新拍加入候选池, 池满后选出互相关最高的一批建初始模板
                        pool = self.boot_pool.setdefault(i, [])
                        pool.append(rs.copy())
                        if len(pool) >= self.BOOTSTRAP_N:
                            # 计算每拍与其他所有拍的平均相关
                            scores = []
                            for idx, ra in enumerate(pool):
                                others = [self._corr(ra, pool[j])
                                          for j in range(len(pool)) if j != idx]
                                scores.append(np.mean(others))
                            # 保留得分 >= 阈值的"共识拍"
                            good = [pool[k] for k, s in enumerate(scores)
                                    if s >= self.BOOTSTRAP_R]
                            if good:
                                # 用共识拍的均值建初始模板
                                self.template[i] = np.mean(good, axis=0)
                                self.nbeats[i] = len(good)
                                self.nrejected[i] = 0
                                self.nresets[i] = self.nresets.get(i, 0)  # 保持计数
                                self.gate_history[i] = []
                                self.boot_pool[i] = []
                                print(f"[avg] ch{i} 冷启动完成: 共识拍 {len(good)}/{self.BOOTSTRAP_N}, "
                                      f"得分 {[f'{s:.2f}' for s in scores]}")
                            else:
                                # 没有共识拍 -> 丢掉最老的一拍, 继续等
                                self.boot_pool[i] = pool[1:]
                                print(f"[avg] ch{i} 冷启动: 无共识拍(得分 {[f'{s:.2f}' for s in scores]}), 继续等待")
                    else:
                        # ---- 正常模式: 门控 + EWMA 更新 ----
                        accept = True
                        if self.nbeats.get(i, 0) >= self.min_beats and self.gate_r > 0:
                            r_val = self._corr(rs, tpl)
                            if r_val < self.gate_r:
                                accept = False
                                self.nrejected[i] = self.nrejected.get(i, 0) + 1
                        if accept:
                            a = self.alpha * getattr(self, '_alpha_scale', 1.0)
                            self.template[i] = (1 - a) * tpl + a * rs
                            self.nbeats[i] = self.nbeats.get(i, 0) + 1
                        # 滚动记录门控结果, 监测拒绝率
                        hist = self.gate_history.setdefault(i, [])
                        hist.append(accept)
                        if len(hist) > self.RESET_WIN:
                            del hist[0]
                        # 自动重置: 窗口满且拒绝率过高 -> 模板可能已漂到错误状态
                        if (len(hist) >= self.RESET_WIN and
                                hist.count(False) / len(hist) >= self.RESET_THRESH):
                            print(f"[avg] ch{i} 拒绝率 {hist.count(False)}/{len(hist)}, 自动重置模板")
                            self._reset_template(i)
                    self.last_pk_abs[i] = pk_abs  # 无论接受/拒绝, 峰位置都标记为已处理
            # 相位对齐(PLL): 把播放相位锚到最近检出的真实峰, 纠正漂移/HRV。
            # 大误差(疑似漏检/重锁)直接跳; 小误差摊到本区间每样本渐进消除, 避免波形跳变。
            last_pk_abs = abs0 + int(peaks[-1])
            samples_since = cur_abs - last_pk_abs
            target = (self.peak_phase + samples_since / P) % 1.0
            span = max(1, int((self.fs or comb.fs or 250) * 0.3))
            with self.lock:
                err = (target - self.phase.get(i, target) + 0.5) % 1.0 - 0.5  # 环绕到[-0.5,0.5)
                if abs(err) > 0.25:
                    self.phase[i] = float(target); self.dphase_extra[i] = 0.0
                else:
                    self.dphase_extra[i] = float(err) / span

    def process(self, trig):
        """输入「提取」输出一帧, 返回同步平均重建一帧。"""
        fs = self.fs or comb.fs or preproc.fs
        f0 = comb.f0
        out = [0.0] * len(trig)
        if not self.enabled or not fs or not f0:
            return out
        conf = comb.f0_confidence     # 0=极低 / 1=低置信 / 2=可用
        P    = fs / f0
        dphi = f0 / fs
        # 启动等待期: 前 startup_delay 秒不收集心拍(跳过ADC基线跳变/高通滤波器瞬态)
        startup_thresh = int(fs * self.startup_delay)
        in_startup = self._startup_samples < startup_thresh
        self._startup_samples += len(trig)
        with self.lock:
            for i, v in enumerate(trig):
                self.abs0.setdefault(i, 0)
                # conf>=1 且已过等待期，才写入心拍缓冲
                if conf >= 1 and not in_startup:
                    b = self.buf.setdefault(i, [])
                    b.append(float(v))
                    self.count[i] = self.count.get(i, 0) + 1
                    cap = int(fs * 6)
                    if len(b) > cap:
                        drop = len(b) - cap
                        del b[:drop]
                        self.abs0[i] += drop
                # 相位跟踪 + 模板播放: 始终进行(保留最后有效模板的连续输出)
                ph = (self.phase.get(i, 0.0) + dphi + self.dphase_extra.get(i, 0.0)) % 1.0
                self.phase[i] = ph
                tpl = self.template.get(i)
                if tpl is not None and self.nbeats.get(i, 0) >= self.min_beats:
                    fidx = ph * self.n_pts
                    i0   = int(fidx) % self.n_pts
                    frac = fidx - int(fidx)
                    out[i] = float(tpl[i0] * (1 - frac) + tpl[(i0 + 1) % self.n_pts] * frac)
        # conf>=1: 触发模板重建; conf=0: 冻结模板(不更新)
        if conf >= 1:
            self._since += len(trig)
            if self._since >= int(fs * 0.3):
                self._since = 0
                self._alpha_scale = 1.0 if conf >= 2 else 0.3  # 低置信时小步长更新
                self._rebuild(fs, P)
        return out


averager = SyncAverager()


# ---------------------------------------------------------------------------
# 共享状态: 生产者(串口/演示)把帧塞进 pending, 刷新任务批量推给客户端
# ---------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.clients = set()            # 已连接的 WebSocket
        self.pending = []               # 待发送的帧, 每帧是一个 float 列表
        self.lock = threading.Lock()
        self.raw_names = None           # 原始通道名, 例如 ["ch0"]
        self.channel_names = None       # 显示通道名 = 原始+提取+平均
        self.forced_names = None        # --names 指定的固定通道名
        self.history = []               # 完整历史帧(从启动到现在, 无上限; 供"保存数据"使用)
        self._bl_sos = None             # 基线低通滤波器(与 CombExtractor 高通互补)
        self._bl_zi  = {}               # 每通道低通滤波器状态

    def _baseline(self, values):
        """对原始信号做低通 0.5Hz, 提取基线分量供显示叠加用。
        截止频率与 CombExtractor 内部高通一致, 保证 基线 + 提取 ≈ 原始。
        push_frame 是单线程调用(生产者), 无需加锁。"""
        fs = preproc.fs
        if not fs:
            return [0.0] * len(values)
        if self._bl_sos is None:
            ny = fs * 0.5
            self._bl_sos = butter(2, 0.5 / ny, btype="low", output="sos")
        out = []
        for i, v in enumerate(values):
            if i not in self._bl_zi:
                self._bl_zi[i] = sosfilt_zi(self._bl_sos) * v
            y, self._bl_zi[i] = sosfilt(self._bl_sos, [v], zi=self._bl_zi[i])
            out.append(float(y[0]))
        return out

    def push_frame(self, values):
        preproc.measure()   # 自动测量采样率
        ext = comb.process(values)
        avg = averager.process(ext)
        # 基线叠加: 把低频慢漂移加回提取/平均, 仅影响显示, 算法内部不变
        bl = self._baseline(values)
        ext_disp = [ext[i] + bl[i] for i in range(len(ext))]
        avg_disp = [avg[i] + bl[i] for i in range(len(avg))]
        with self.lock:
            if self.raw_names is None:
                if self.forced_names:
                    self.raw_names = list(self.forced_names)
                else:
                    self.raw_names = [f"ch{i}" for i in range(len(values))]
                self.channel_names = (self.raw_names
                                      + [f"{n}(提取)" for n in self.raw_names]
                                      + [f"{n}(平均)" for n in self.raw_names])
            frame = list(values) + list(ext_disp) + list(avg_disp)
            self.history.append(frame)   # 写入滚动历史缓冲
            self.pending.append(frame)

    def drain(self):
        with self.lock:
            frames = self.pending
            self.pending = []
            return frames

    def save_buffer(self):
        """把当前滚动缓冲里的历史数据一次性存为 CSV + 频谱。返回 (文件路径, 样本数)。"""
        with self.lock:
            if not self.history:
                print("[rec] 缓冲区为空, 跳过保存")
                return None, 0
            rows  = list(self.history)
            names = list(self.channel_names or ["ch0"])
        rec_dir = Path(__file__).parent / "recordings"
        rec_dir.mkdir(exist_ok=True)
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = rec_dir / f"rec_{ts}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(names)
            for row in rows:
                w.writerow([f"{v:.6g}" for v in row])
        count = len(rows)
        arr   = np.asarray(rows, dtype=float)
        try:
            self._save_spectrum(str(path), arr, names)
        except Exception as e:
            print(f"[rec] 频谱保存失败: {e}")
        print(f"[rec] 已保存缓冲区, {count} 个样本 -> {path}")
        return str(path), count

    def clear_buffer(self):
        """清空历史缓冲区(前端"清除数据"命令)。"""
        with self.lock:
            count = len(self.history)
            self.history = []
        print(f"[rec] 已清除缓冲区 ({count} 个样本)")
        return count

    def _save_spectrum(self, ts_path, arr, names):
        """对每一列(原始/滤波/提取)用 Welch 算功率谱, 存到 *_spectrum.csv。"""
        if arr.ndim != 2 or arr.shape[0] < 64:
            print("[rec] 样本太少, 跳过频谱")
            return
        fs = preproc.fs or 250.0
        nper = int(min(4096, arr.shape[0]))
        cols, freq = {}, None
        for j, nm in enumerate(names):
            if j >= arr.shape[1]:
                break
            fr, pp = welch(arr[:, j] - arr[:, j].mean(), fs=fs, nperseg=nper)
            freq = fr
            cols[f"{nm}_psd"] = pp
        spec_path = ts_path[:-4] + "_spectrum.csv"
        with open(spec_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["freq_Hz"] + list(cols.keys()))
            for i in range(len(freq)):
                w.writerow([f"{freq[i]:.4f}"] + [f"{cols[k][i]:.6g}" for k in cols])
        print(f"[rec] 频谱已存 (fs={fs:.1f}Hz, nperseg={nper}) -> {spec_path}")


hub = Hub()


# ---------------------------------------------------------------------------
# FireWater 文本解析: 字节流按 \n 分帧, 每帧按 ',' 拆成多个浮点
# ---------------------------------------------------------------------------
class FireWaterParser:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk: bytes):
        """喂入原始字节, 返回解析出的若干帧(每帧为 float 列表)。"""
        self.buf.extend(chunk)
        frames = []
        while True:
            idx = self.buf.find(b"\n")
            if idx < 0:
                break
            line = self.buf[:idx]
            del self.buf[:idx + 1]
            line = line.strip().rstrip(b",")
            if not line:
                continue
            try:
                vals = [float(x) for x in line.split(b",") if x.strip() != b""]
            except ValueError:
                continue  # 跳过乱码/半截行
            if vals:
                frames.append(vals)
        return frames


# ---------------------------------------------------------------------------
# 串口连接状态管理(支持从网页动态选择/切换 COM 口)
# ---------------------------------------------------------------------------
_serial_state = {"connected": False, "port": None, "baud": 115200, "error": None}
_serial_stop: threading.Event | None = None
_serial_lock = threading.Lock()
# 用于把后台线程产生的状态变更通知 flusher 广播
_pending_serial_notify: list = []
_notify_lock = threading.Lock()

def _push_serial_notify():
    with _notify_lock:
        _pending_serial_notify.append(dict(_serial_state))

def serial_connect(port: str, baud: int):
    """在网页或命令行发起串口连接。"""
    global _serial_stop
    with _serial_lock:
        if _serial_stop is not None:
            _serial_stop.set()
        stop = threading.Event()
        _serial_stop = stop
    _serial_state.update({"port": port, "baud": baud, "connected": False, "error": None})
    _push_serial_notify()
    threading.Thread(target=serial_reader, args=(port, baud, stop), daemon=True).start()

def serial_disconnect():
    global _serial_stop
    with _serial_lock:
        if _serial_stop: _serial_stop.set(); _serial_stop = None
    _serial_state.update({"connected": False, "error": None})
    _push_serial_notify()
    print("[serial] 已断开")


# ---------------------------------------------------------------------------
# 数据源: 真实串口
# ---------------------------------------------------------------------------
def serial_reader(port: str, baud: int, stop_event: threading.Event | None = None):
    import serial  # pyserial
    parser = FireWaterParser()
    def stopped(): return stop_event is not None and stop_event.is_set()
    while not stopped():
        ser = None
        try:
            ser = serial.Serial(port, baud, timeout=1)
            print(f"[serial] 已打开 {port} @ {baud}")
            _serial_state.update({"connected": True, "error": None})
            _push_serial_notify()
            last = time.time()
            while not stopped():
                chunk = ser.read(ser.in_waiting or 1)
                if chunk:
                    last = time.time()
                    for f in parser.feed(chunk):
                        hub.push_frame(f)
                elif time.time() - last > 8:
                    print("[serial] 8 秒无数据, 重连端口...")
                    break
        except Exception as e:
            if stopped(): break
            msg = str(e)
            print(f"[serial] 错误: {msg}; 2 秒后重试...")
            _serial_state.update({"connected": False, "error": msg})
            _push_serial_notify()
            time.sleep(2)
        finally:
            try:
                if ser: ser.close()
            except Exception:
                pass
    _serial_state.update({"connected": False})
    _push_serial_notify()


# ---------------------------------------------------------------------------
# 数据源: CSV 回放(演示)
# ---------------------------------------------------------------------------
def csv_replay(path: str, rate: float, loop_play: bool):
    p = Path(path)
    with p.open("r", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        print("[demo] CSV 为空")
        return
    # 第一行若不是数字, 当作通道名
    header = rows[0]
    try:
        float(header[0])
        names = [f"ch{i}" for i in range(len(header))]
        data_rows = rows
    except ValueError:
        names = header
        data_rows = rows[1:]
    if hub.forced_names:
        names = list(hub.forced_names)
    hub.forced_names = names  # 让 push_frame 据此构造 原始+滤波 通道
    print(f"[demo] 回放 {len(data_rows)} 帧, 通道={names}, 速率={rate}/s")

    interval = 1.0 / rate if rate > 0 else 0
    while True:
        for row in data_rows:
            try:
                vals = [float(x) for x in row if x.strip() != ""]
            except ValueError:
                continue
            if vals:
                hub.push_frame(vals)
            if interval:
                time.sleep(interval)
        if not loop_play:
            print("[demo] 回放结束")
            break


# ---------------------------------------------------------------------------
# WebSocket / HTTP
# ---------------------------------------------------------------------------
async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    hub.clients.add(ws)
    # 连接时先发一次元信息(通道名 + 当前录制状态)
    await ws.send_str(json.dumps({"type": "meta",
                                  "channels": hub.channel_names or ["ch0"]}))
    await ws.send_str(json.dumps({"type": "rec", "on": False, "path": None, "count": 0}))
    await ws.send_str(json.dumps({"type": "serial_state", **_serial_state}))
    await ws.send_str(json.dumps(preproc.status()))   # 当前采样率
    await ws.send_str(json.dumps(comb.status()))       # 当前提取设置
    await ws.send_str(json.dumps(averager.status()))  # 当前同步平均设置
    print(f"[ws] 客户端连接, 当前 {len(hub.clients)} 个")
    try:
        async for msg in ws:  # 接收客户端命令
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            cmd = data.get("cmd")
            if cmd == "save_buffer":
                # 在线程池里执行(文件IO + 频谱计算可能耗时)
                loop = asyncio.get_event_loop()
                path, count = await loop.run_in_executor(None, hub.save_buffer)
                await broadcast_rec(on=False, path=path, count=count)
            elif cmd == "extract":
                comb.update(enabled=data.get("enabled"), n_harm=data.get("n_harm"),
                            bw=data.get("bw"))
                await broadcast_json(comb.status())
            elif cmd == "average":
                averager.update(enabled=data.get("enabled"), alpha=data.get("alpha"))
                await broadcast_json(averager.status())
            elif cmd == "connect":
                port = (data.get("port") or "").strip()
                baud = int(data.get("baud") or 115200)
                if port:
                    serial_connect(port, baud)
                await broadcast_json({"type": "serial_state", **_serial_state})
            elif cmd == "disconnect":
                serial_disconnect()
                await broadcast_json({"type": "serial_state", **_serial_state})
            elif cmd == "average_reset":
                averager.reset()
                await broadcast_json(averager.status())
            elif cmd == "clear_buffer":
                hub.clear_buffer()
                await broadcast_rec(on=False, path=None, count=0)
    finally:
        hub.clients.discard(ws)
        print(f"[ws] 客户端断开, 剩余 {len(hub.clients)} 个")
    return ws


async def index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def ports_handler(request):
    """返回当前系统可用串口列表(供网页下拉选择)。"""
    try:
        from serial.tools import list_ports
        ports = [{"device": p.device, "description": p.description}
                 for p in list_ports.comports()]
    except Exception as e:
        ports = []; print(f"[ports] 枚举失败: {e}")
    return web.json_response(ports)


async def broadcast_json(obj):
    """向所有客户端广播一个 JSON 消息。"""
    msg = json.dumps(obj)
    for ws in list(hub.clients):
        try:
            await ws.send_str(msg)
        except Exception:
            hub.clients.discard(ws)


async def broadcast_rec(on, path, count):
    """向所有客户端广播录制状态。"""
    await broadcast_json({"type": "rec", "on": on, "path": path, "count": count})


async def flusher(app):
    """每 ~33ms 把缓冲的帧批量推给所有客户端(约 30fps)。"""
    last_meta = None
    last_hr = None
    last_beats = None
    last_fs = None
    tick = 0
    while True:
        await asyncio.sleep(0.033)
        # 把后台串口线程产生的状态变更推给所有客户端
        with _notify_lock:
            notifs = list(_pending_serial_notify); _pending_serial_notify.clear()
        for n in notifs:
            if hub.clients:
                await broadcast_json({"type": "serial_state", **n})
        if hub.channel_names and hub.channel_names != last_meta and hub.clients:
            last_meta = list(hub.channel_names)
            await broadcast_json({"type": "meta", "channels": hub.channel_names})
        tick += 1
        if tick >= 30 and hub.clients:
            tick = 0
            if comb.f0 != last_hr:
                last_hr = comb.f0
                await broadcast_json(comb.status())
            nb = averager.nbeats.get(0, 0)
            if nb != last_beats:
                last_beats = nb
                await broadcast_json(averager.status())
            if preproc.fs != last_fs:
                last_fs = preproc.fs
                await broadcast_json(preproc.status())
        frames = hub.drain()
        if not frames or not hub.clients:
            continue
        payload = {"type": "data", "frames": frames}
        msg = json.dumps(payload)
        for ws in list(hub.clients):
            try:
                await ws.send_str(msg)
            except Exception:
                hub.clients.discard(ws)


async def on_startup(app):
    app["flusher"] = asyncio.create_task(flusher(app))


async def on_cleanup(app):
    app["flusher"].cancel()


def main():
    ap = argparse.ArgumentParser(description="波形上位机桥接服务器")
    ap.add_argument("--serial", help="蓝牙串口/COM 口, 例如 COM5 或 /dev/rfcomm0")
    ap.add_argument("--baud", type=int, default=115200, help="波特率(默认115200)")
    ap.add_argument("--demo", help="回放的 CSV 路径(演示模式)")
    ap.add_argument("--rate", type=float, default=300, help="演示回放速率 样本/秒")
    ap.add_argument("--loop", action="store_true", help="演示模式循环回放")
    ap.add_argument("--names", help="通道名, 逗号分隔, 例如 I0 或 I0,U0,P0")
    ap.add_argument("--fs", type=float, help="采样率Hz(默认自动测量)")
    ap.add_argument("--no-extract", action="store_true", help="启动时关闭谐波梳状提取")
    ap.add_argument("--harm", type=int, default=5, help="提取谐波个数(默认5)")
    ap.add_argument("--bw", type=float, default=0.4, help="每条梳齿带宽Hz(默认0.4)")
    ap.add_argument("--no-avg", action="store_true", help="启动时关闭同步平均")
    ap.add_argument("--avg-alpha", type=float, default=0.2,
                    help="同步平均EWMA系数(默认0.2, 越小越平滑; N_eff≈(2-α)/α)")
    ap.add_argument("--avg-gate", type=float, default=0.2,
                    help="拍质量门控阈值(默认0.2; 新拍与模板相关系数低于此值则拒绝; 0=关闭门控)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    if args.names:
        hub.forced_names = [n.strip() for n in args.names.split(",") if n.strip()]

    if args.fs:
        preproc.set_fs(args.fs)
    comb.update(enabled=not args.no_extract, n_harm=args.harm, bw=args.bw, fs=args.fs)
    averager.update(enabled=not args.no_avg, alpha=args.avg_alpha,
                    gate_r=args.avg_gate, fs=args.fs)

    if args.serial:
        serial_connect(args.serial, args.baud)
    elif args.demo:
        threading.Thread(target=csv_replay,
                         args=(args.demo, args.rate, args.loop),
                         daemon=True).start()
    else:
        print("提示: 未指定 --serial 或 --demo, 网页可打开但没有数据。")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/ports", ports_handler)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    print(f"\n网页地址:  http://localhost:{args.port}")
    print(f"手机访问:  http://<本机局域网IP>:{args.port}  (需与电脑同一WiFi)\n")
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
