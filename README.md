# 波形上位机（网页版）

手机/电脑浏览器打开即可实时显示波形，无需安装任何 App。装置通过 **JDY-31 蓝牙（经典 SPP）** 发送数据。

```
装置 ──蓝牙SPP──> 电脑（server.py 读COM口）──WebSocket──> 手机/电脑浏览器
```

手机和电脑只要在**同一 WiFi** 下，打开网址即可。

---

## 快速开始

### 方式一：下载打包版（推荐，无需 Python）

1. 下载 [`dist/waveform_web.zip`](dist/waveform_web.zip)，解压
2. 双击 `启动-真实硬件.bat` 或 `启动-演示模式.bat`
3. 浏览器自动打开，或手动访问 `http://localhost:8090`

> 仅支持 Windows。

### 方式二：源码运行

**环境要求**：Python 3.8+

```bash
git clone https://github.com/ningL247/waveform_web.git
cd waveform_web
pip install -r requirements.txt
```

---

## 使用说明

### 演示模式（不接硬件，用 CSV 回放）

```bash
python server.py --demo data/ch0.csv --rate 300 --loop
```

或直接双击 `启动-演示模式.bat`。

### 真实硬件模式（接 JDY-31）

1. Windows「蓝牙和其他设备」里配对 JDY-31，记下分配的**传出 COM 口**（如 COM8）
2. 双击 `启动-真实硬件.bat`，在网页右侧面板选择对应 COM 口

或命令行：

```bash
python server.py --serial COM8 --baud 115200
```

> JDY-31 默认波特率有时为 9600，没有数据时把 `--baud` 改成 9600。

### 打开网页

| 设备 | 地址 |
|------|------|
| 本机 | http://localhost:8090 |
| 同 WiFi 手机/其他电脑 | http://\<电脑局域网IP\>:8090 |

> 手机连不上时，在电脑防火墙放行 8090 端口（或临时关闭专用网络防火墙）。

---

## 参数说明

| 参数 | 说明 |
|------|------|
| `--serial COM8` | 蓝牙串口/COM 口 |
| `--baud 115200` | 波特率 |
| `--demo 路径.csv` | 回放 CSV（演示模式） |
| `--rate 300` | 演示回放速率（样本/秒） |
| `--loop` | 演示循环回放 |
| `--port 8090` | 服务端口（默认 8090） |
| `--fs 250` | 强制指定采样率（Hz） |

---

## 数据格式（VOFA+ FireWater）

串口每行一帧，多通道用逗号分隔，`\n` 结尾：

```
1048.27\n              ← 单通道
1.2,3.4,5.6\n          ← 三通道
```

网页自动按通道数绘制多条曲线。

---

## 网页功能

- **暂停/继续**：冻结当前波形
- **自动量程**：关闭后切换为固定量程
- **窗口大小**：调整显示最近多少个采样点
- 实时显示采样率与各通道当前值

---

## 为什么需要「桥接端」

浏览器的 Web Bluetooth 只支持 BLE，**无法连接 JDY-31 这种经典蓝牙 SPP**（iOS 完全不支持）。因此需要一台 Windows 电脑将 JDY-31 配对为串口，再通过 WebSocket 转发给浏览器。
