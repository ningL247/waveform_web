# -*- coding: utf-8 -*-
"""探测哪个 COM 口、哪个波特率有数据。装置需开机正在发数据。"""
import serial, time

for port in ["COM8", "COM6"]:
    for baud in [9600, 115200]:
        try:
            s = serial.Serial(port, baud, timeout=1)
        except Exception as e:
            print(f"[{port} @ {baud}] 打不开: {e}")
            continue
        time.sleep(2.0)
        n = s.in_waiting
        data = s.read(n or 1)
        s.close()
        print(f"[{port} @ {baud}] 收到 {len(data)} 字节: {data[:80]!r}")
