#!/usr/bin/env python3
"""Phase 4.1 Metrics Collector"""

import subprocess
import time
import json
import re
from datetime import datetime

class MetricsCollector:
    def __init__(self, serial="d9a9eec8"):
        self.serial = serial
        self.start_time = time.time()
        self.metrics = {}
        
    def start_capture(self):
        """Start capturing metrics from various sources"""
        self.start_time = time.time()
        self.logcat_proc = subprocess.Popen(
            ["adb", "-s", self.serial, "logcat", "-s", "USBDISP"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1
        )
        self.gst_log = open(f"/tmp/phase41_gst_{int(time.time())}.log", "w")
        
    def parse_usbdisp_line(self, line):
        """Parse USBDISP log line for metrics"""
        # RX: frames=X rendered=Y gaps=Z crc=A badlen=B badcodec=B decode_errors=D dropped=F fps=X.Y WxH
        match = re.search(r'RX: frames=(\d+) rendered=(\d+) gaps=(\d+) crc=(\d+) badlen=(\d+) badcodec=(\d+) decode_errors=(\d+) dropped=(\d+) fps=([\d.]+) (\d+)x(\d+)', line)
        if match:
            return {
                "frames": int(match.group(1)),
                "rendered": int(match.group(2)),
                "gaps": int(match.group(3)),
                "crc": int(match.group(4)),
                "badlen": int(match.group(5)),
                "badcodec": int(match.group(6)),
                "decode_errors": int(match.group(7)),
                "dropped": int(match.group(8)),
                "fps": float(match.group(9)),
                "width": int(match.group(9)),
                "height": int(match.group(10))
            }
        return None
    
    def collect(self, duration=60):
        """Collect metrics for specified duration"""
        end_time = time.time() + duration
        while time.time() < end_time:
            line = self.logcat_proc.stdout.readline()
            if not line:
                break
            parsed = self.parse_usbdisp_line(line)
            if parsed:
                self.metrics.setdefault("android", []).append(parsed)
        return self.metrics

if __name__ == "__main__":
    collector = MetricsCollector()
    collector.start_capture()
    metrics = collector.collect(60)
    print(json.dumps(metrics, indent=2))