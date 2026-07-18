#!/usr/bin/env python3
"""Scan the Feetech STS3215 bus: which servo ids answer a position read?

Run with base_host stopped (it owns the port). Uses an exclusive open so a
crash-looping base_host can't corrupt the scan. All silent = no servo power or
the data trunk to the first servo is unplugged; a single id silent = that node.

  sudo systemctl stop base_host
  /home/jatson/miniconda3/envs/lerobot/bin/python ~/probe_bus.py
  sudo systemctl start base_host
"""
import sys
import time

import serial

PORT = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B61036495-if00"
BAUD = 1000000


def cksum(b):
    return (~sum(b)) & 0xFF


def read_pos(ser, sid):
    body = [sid, 0x04, 0x02, 0x38, 0x02]      # read 2 bytes from reg 56 (pos)
    ser.reset_input_buffer()
    ser.write(bytes([0xFF, 0xFF] + body + [cksum(body)]))
    ser.flush()
    time.sleep(0.004)
    r = ser.read(64)
    i = r.find(bytes([0xFF, 0xFF, sid]))
    if i < 0 or len(r) < i + 7:
        return None
    return (r[i + 5] | (r[i + 6] << 8)) & 0x0FFF


def main():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.02, exclusive=True)
    except Exception as e:  # noqa: BLE001 — surface any open failure plainly
        print(f"open FAILED: {e}  (base_host 还占着口? 先 sudo systemctl stop base_host)")
        sys.exit(1)
    print(f"port open OK: {PORT}")
    ans = {}
    for sid in range(1, 10):                   # arm 1-6, wheels 7-9
        v = None
        for _ in range(3):
            v = read_pos(ser, sid)
            if v is not None:
                break
            time.sleep(0.01)
        ans[sid] = v
        tag = "arm " if sid <= 6 else "WHEEL"
        print(f"  id {sid} [{tag}]: {'pos=' + str(v) if v is not None else 'NO ANSWER'}")
    alive = [s for s, v in ans.items() if v is not None]
    print(f"answered: {alive if alive else 'NONE — 整条总线无响应(检查 12V / 主干线)'}")
    ser.close()


if __name__ == "__main__":
    main()
