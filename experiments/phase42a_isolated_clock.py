#!/usr/bin/env python3
"""Phase 4.2A: isolated clock-driven latest-frame repetition prototype.

This intentionally does not use the production encoder, transport, Android
receiver, or launcher.  A PipeWire/appsink pipeline and appsrc/fakesink
pipeline are independent; application-owned bytes are the only bridge.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import signal
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from bridge.gnome_capture import PortalError, ScreenCastPortal  # noqa: E402


@dataclass(frozen=True)
class CachedFrame:
    payload: bytes
    generation: int
    source_ns: int


class LatestFrameCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._available = threading.Event()
        self._frame: CachedFrame | None = None
        self._generation = 0

    def replace(self, payload: bytes, source_ns: int) -> CachedFrame:
        with self._lock:
            self._generation += 1
            self._frame = CachedFrame(payload, self._generation, source_ns)
            frame = self._frame
        self._available.set()
        return frame

    def wait(self, timeout: float) -> bool:
        return self._available.wait(timeout)

    def snapshot(self) -> CachedFrame | None:
        with self._lock:
            return self._frame


class Metrics:
    def __init__(self, phases: list[tuple[str, float]]) -> None:
        self._lock = threading.Lock()
        self.phases = phases
        self.first_source_ns: int | None = None
        self.first_output_ns: int | None = None
        self.source: list[tuple[int, int]] = []
        self.push: list[tuple[int, int, bool]] = []
        self.handoff: list[int] = []

    def phase_at(self, stamp_ns: int) -> str:
        with self._lock:
            start = self.first_output_ns
        if start is None or stamp_ns < start:
            return "startup"
        elapsed = (stamp_ns - start) / 1_000_000_000
        boundary = 0.0
        for name, duration in self.phases:
            boundary += duration
            if elapsed < boundary:
                return name
        return "complete"

    def add_source(self, stamp_ns: int, generation: int) -> None:
        with self._lock:
            if self.first_source_ns is None:
                self.first_source_ns = stamp_ns
            self.source.append((stamp_ns, generation))

    def add_push(self, stamp_ns: int, generation: int, repeated: bool) -> None:
        with self._lock:
            if self.first_output_ns is None:
                self.first_output_ns = stamp_ns
            self.push.append((stamp_ns, generation, repeated))

    def add_handoff(self, stamp_ns: int) -> None:
        with self._lock:
            self.handoff.append(stamp_ns)

    @staticmethod
    def interval_stats(stamps: list[int]) -> dict[str, float | int | None]:
        intervals = [(b - a) / 1_000_000 for a, b in zip(stamps, stamps[1:])]
        if not intervals:
            return {
                "samples": 0,
                "mean_ms": None,
                "min_ms": None,
                "max_ms": None,
                "p95_ms": None,
                "p99_ms": None,
            }
        ordered = sorted(intervals)

        def percentile(p: float) -> float:
            index = min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1)
            return ordered[index]

        return {
            "samples": len(intervals),
            "mean_ms": statistics.fmean(intervals),
            "min_ms": min(intervals),
            "max_ms": max(intervals),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
        }

    def report(self) -> dict[str, Any]:
        with self._lock:
            first_source = self.first_source_ns
            first_output = self.first_output_ns
            source = list(self.source)
            push = list(self.push)
            handoff = list(self.handoff)
        result: dict[str, Any] = {
            "first_output_after_source_ms": (
                (first_output - first_source) / 1_000_000
                if first_source is not None and first_output is not None
                else None
            ),
            "source_frames_total": len(source),
            "output_frames_total": len(push),
            "new_frame_outputs_total": sum(not item[2] for item in push),
            "repeated_frame_outputs_total": sum(item[2] for item in push),
            "push_cadence": self.interval_stats([item[0] for item in push]),
            "handoff_cadence": self.interval_stats(handoff),
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "phases": {},
        }
        if first_output is None:
            return result
        phase_start = first_output
        for name, duration in self.phases:
            phase_end = phase_start + round(duration * 1_000_000_000)
            phase_source = [item for item in source if phase_start <= item[0] < phase_end]
            phase_push = [item for item in push if phase_start <= item[0] < phase_end]
            phase_handoff = [item for item in handoff if phase_start <= item < phase_end]
            elapsed = (
                (phase_push[-1][0] - phase_push[0][0]) / 1_000_000_000
                if len(phase_push) > 1
                else 0.0
            )
            result["phases"][name] = {
                "configured_duration_s": duration,
                "source_frames": len(phase_source),
                "output_frames": len(phase_push),
                "mean_output_fps": ((len(phase_push) - 1) / elapsed if elapsed > 0 else None),
                "new_frame_outputs": sum(not item[2] for item in phase_push),
                "repeated_frame_outputs": sum(item[2] for item in phase_push),
                "push_cadence": self.interval_stats([item[0] for item in phase_push]),
                "handoff_cadence": self.interval_stats(phase_handoff),
            }
            phase_start = phase_end
        return result


def parse_phase(value: str) -> tuple[str, float]:
    try:
        name, raw_duration = value.split(":", 1)
        duration = float(raw_duration)
    except ValueError as error:
        raise argparse.ArgumentTypeError("phase must be NAME:SECONDS") from error
    if not name or duration <= 0:
        raise argparse.ArgumentTypeError("phase name and positive duration required")
    return name, duration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=600)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--cursor-mode", choices=("embedded", "metadata", "hidden", "auto"), default="embedded")
    parser.add_argument(
        "--phase",
        action="append",
        type=parse_phase,
        dest="phases",
        help="measured phase NAME:SECONDS; repeat for multiple phases",
    )
    parser.add_argument("--metrics-json", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    phases = args.phases or [("active", 20.0), ("static", 120.0), ("recovery", 20.0)]
    total_duration = sum(duration for _name, duration in phases)
    Gst.init(None)
    cache = LatestFrameCache()
    metrics = Metrics(phases)
    stop_event = threading.Event()
    loop = GLib.MainLoop()
    portal = ScreenCastPortal(cursor_mode=args.cursor_mode)
    source_pipeline: Gst.Pipeline | None = None
    output_pipeline: Gst.Pipeline | None = None
    pipewire_fd = -1
    scheduler: threading.Thread | None = None
    exit_code = 0

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()
        loop.quit()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        portal.create_session()
        portal.select_virtual_source()
        node_id = portal.start()
        pipewire_fd = portal.open_pipewire_remote()
        print(f"EVENT portal_ready node={node_id} fd={pipewire_fd}", flush=True)

        raw_caps = (
            f"video/x-raw,format=I420,width={args.width},height={args.height},"
            "pixel-aspect-ratio=1/1"
        )
        source_pipeline = Gst.parse_launch(
            " ".join(
                [
                    f"pipewiresrc fd={pipewire_fd} path={node_id} do-timestamp=true",
                    "! video/x-raw",
                    "! videoconvert",
                    "! videoscale",
                    f"! {raw_caps}",
                    "! appsink name=source_sink emit-signals=true max-buffers=1 drop=true sync=false",
                ]
            )
        )
        output_pipeline = Gst.parse_launch(
            " ".join(
                [
                    "appsrc name=output_source is-live=true format=time block=true max-buffers=2",
                    f"caps={raw_caps},framerate={args.fps}/1",
                    "! fakesink name=output_sink sync=true signal-handoffs=true",
                ]
            )
        )
        if not isinstance(source_pipeline, Gst.Pipeline) or not isinstance(output_pipeline, Gst.Pipeline):
            raise RuntimeError("GStreamer did not create both pipelines")

        source_sink = source_pipeline.get_by_name("source_sink")
        output_source = output_pipeline.get_by_name("output_source")
        output_sink = output_pipeline.get_by_name("output_sink")
        if source_sink is None or output_source is None or output_sink is None:
            raise RuntimeError("required named GStreamer element missing")

        def on_sample(sink: Gst.Element) -> Gst.FlowReturn:
            sample = sink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.ERROR
            buffer = sample.get_buffer()
            if buffer is None:
                return Gst.FlowReturn.ERROR
            stamp = time.monotonic_ns()
            payload = buffer.extract_dup(0, buffer.get_size())
            frame = cache.replace(payload, stamp)
            metrics.add_source(stamp, frame.generation)
            if frame.generation == 1:
                print(
                    f"EVENT first_source_frame generation=1 bytes={len(payload)} monotonic_ns={stamp}",
                    flush=True,
                )
            return Gst.FlowReturn.OK

        source_sink.connect("new-sample", on_sample)
        output_sink.connect("handoff", lambda *_args: metrics.add_handoff(time.monotonic_ns()))

        def on_bus_message(_bus: Gst.Bus, message: Gst.Message, pipeline_name: str) -> None:
            nonlocal exit_code
            if message.type == Gst.MessageType.ERROR:
                error, details = message.parse_error()
                print(f"ERROR pipeline={pipeline_name} error={error} details={details}", flush=True)
                exit_code = 1
                stop_event.set()
                loop.quit()
            elif message.type == Gst.MessageType.EOS and pipeline_name == "output":
                print("EVENT output_eos", flush=True)
                loop.quit()

        for pipeline_name, pipeline in (("source", source_pipeline), ("output", output_pipeline)):
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", on_bus_message, pipeline_name)

        if output_pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("output pipeline failed to enter PLAYING")
        if source_pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("source pipeline failed to enter PLAYING")

        def run_scheduler() -> None:
            nonlocal exit_code
            while not stop_event.is_set() and not cache.wait(0.1):
                pass
            if stop_event.is_set():
                return
            period_ns = 1_000_000_000 / args.fps
            start_ns = time.monotonic_ns()
            last_generation: int | None = None
            phase_boundary = 0.0
            phase_index = -1
            frame_index = 0
            while not stop_event.is_set():
                deadline = start_ns + round(frame_index * period_ns)
                remaining_ns = deadline - time.monotonic_ns()
                if remaining_ns > 0:
                    stop_event.wait(remaining_ns / 1_000_000_000)
                    if stop_event.is_set():
                        break
                now = time.monotonic_ns()
                elapsed = (now - start_ns) / 1_000_000_000
                if elapsed >= total_duration:
                    break
                while phase_index + 1 < len(phases) and elapsed >= phase_boundary:
                    phase_index += 1
                    phase_boundary += phases[phase_index][1]
                    print(
                        f"EVENT phase_begin name={phases[phase_index][0]} "
                        f"duration_s={phases[phase_index][1]:.3f}",
                        flush=True,
                    )
                frame = cache.snapshot()
                if frame is None:
                    continue
                buffer = Gst.Buffer.new_allocate(None, len(frame.payload), None)
                buffer.fill(0, frame.payload)
                clock = output_pipeline.get_clock()
                if clock is not None:
                    buffer.pts = max(0, clock.get_time() - output_pipeline.get_base_time())
                else:
                    buffer.pts = round(frame_index * Gst.SECOND / args.fps)
                buffer.dts = Gst.CLOCK_TIME_NONE
                buffer.duration = round(Gst.SECOND / args.fps)
                repeated = last_generation == frame.generation
                push_stamp = time.monotonic_ns()
                result = output_source.emit("push-buffer", buffer)
                if result != Gst.FlowReturn.OK:
                    print(f"ERROR push_buffer flow={result}", flush=True)
                    exit_code = 1
                    stop_event.set()
                    GLib.idle_add(loop.quit)
                    return
                metrics.add_push(push_stamp, frame.generation, repeated)
                if frame_index == 0:
                    print(f"EVENT first_output_frame monotonic_ns={push_stamp}", flush=True)
                last_generation = frame.generation
                frame_index += 1
            output_source.emit("end-of-stream")

        scheduler = threading.Thread(target=run_scheduler, name="phase42a-clock", daemon=True)
        scheduler.start()
        loop.run()
    except (GLib.Error, PortalError, RuntimeError, KeyError) as error:
        print(f"ERROR prototype_failed error={error}", flush=True)
        exit_code = 1
    finally:
        stop_event.set()
        if scheduler is not None:
            scheduler.join(timeout=3)
        if source_pipeline is not None:
            source_pipeline.set_state(Gst.State.NULL)
        if output_pipeline is not None:
            output_pipeline.set_state(Gst.State.NULL)
        portal.close()
        if pipewire_fd >= 0:
            os.close(pipewire_fd)

    report = metrics.report()
    report["result"] = "completed" if exit_code == 0 else "failed"
    report["fps_target"] = args.fps
    report["resolution"] = f"{args.width}x{args.height}"
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print("METRICS_JSON_BEGIN", flush=True)
    print(encoded, flush=True)
    print("METRICS_JSON_END", flush=True)
    if args.metrics_json is not None:
        args.metrics_json.write_text(encoded + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
