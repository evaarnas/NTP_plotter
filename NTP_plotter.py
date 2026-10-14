import subprocess
import ctypes
import json
import msvcrt
import os
import re
import sys
import time
import math
import socket
import struct
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import MaxNLocator
from matplotlib.widgets import TextBox, Button


NTP_SERVER = "time.windows.com"
CAP_PERCENTILE = 95.0
CAP_HEADROOM_MULTIPLIER = 1.25

# (elapsed time in seconds, sampling interval in seconds)
SAMPLING_SCHEDULE = [
    (10 * 60, 2),          # first 10 min: every 2 s
    (30 * 60, 5),          # 10–30 min: every 5 s
    (60 * 60, 10),         # 30–60 min: every 10 s
    (3 * 60 * 60, 30),     # 1–3 h: every 30 s
    (12 * 60 * 60, 60),    # 3–12 h: every minute
    (24 * 60 * 60, 120),   # 12–24 h: every 2 min
    (float("inf"), 300),   # >24 h: every 5 min
]


def get_sampling_interval(elapsed):
    for limit, interval in SAMPLING_SCHEDULE:
        if elapsed < limit:
            return interval

    return 300


def get_ntp_sample(server):
    """Return (sample_time, offset_ms, error)."""

    try:
        result = subprocess.run(
            [
                "w32tm",
                "/stripchart",
                f"/computer:{server}",
                "/samples:1",
                "/dataonly"
            ],
            capture_output=True,
            text=True
        )
    except Exception as e:
        return None, None, str(e)

    pattern = re.compile(
        r"(\d\d:\d\d:\d\d),\s*([+-]?\d+\.\d+)s"
    )

    for line in result.stdout.splitlines():
        match = pattern.match(line.strip())

        if match:
            time_str = match.group(1)
            offset = -1 * float(match.group(2)) * 1000 # For some reason, ahead of time is negative and behind is positive

            sample_time = datetime.strptime(
                time_str,
                "%H:%M:%S"
            )

            return sample_time, offset, None

    # No valid NTP sample was found.
    error = result.stderr.strip()

    if not error:
        error = result.stdout.strip()

    if not error:
        error = f"w32tm returned exit code {result.returncode}"

    return None, None, error


def get_packet_rtt(server):
    """Return the UDP NTP request/reply time in milliseconds, or an error."""
    request = bytearray(48)
    request[0] = 0x23  # LI=0, NTP version 4, client mode
    ntp_time = time.time() + 2_208_988_800
    seconds = int(ntp_time)
    fraction = int((ntp_time - seconds) * (1 << 32))
    request[40:48] = struct.pack("!II", seconds, fraction)

    try:
        addresses = socket.getaddrinfo(
            server, 123, type=socket.SOCK_DGRAM
        )
        last_error = "No NTP server address found"

        for family, socktype, protocol, _, address in addresses:
            try:
                with socket.socket(family, socktype, protocol) as sock:
                    sock.settimeout(2.0)
                    start = time.perf_counter()
                    sock.sendto(request, address)
                    response, _ = sock.recvfrom(512)
                    rtt_ms = (time.perf_counter() - start) * 1000

                if len(response) < 48 or (response[0] & 0x07) != 4:
                    last_error = "Invalid NTP server response"
                    continue

                return rtt_ms, None
            except OSError as e:
                last_error = str(e)

        return None, last_error
    except OSError as e:
        return None, str(e)


LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "ntp_plotter.lock",
)


def acquire_instance_lock():
    """Hold an exclusive OS lock; Windows releases it if the process dies."""
    lock_handle = open(LOCK_FILE, "a+b")
    try:
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock_handle.close()
        message = (
            "NTP Plotter is already running.\n\n"
            "Running two instances would mix their samples in the shared "
            "history file. Close the other instance first."
        )
        print(message)
        ctypes.windll.user32.MessageBoxW(
            None, message, "NTP Plotter", 0x10  # MB_ICONERROR
        )
        sys.exit(1)
    return lock_handle


instance_lock = acquire_instance_lock()

plt.ion()

fig, ax = plt.subplots()
fig.set_size_inches(12, 8)

ax.xaxis.set_major_formatter(
    mdates.DateFormatter("%H:%M:%S")
)

ax.set_ylabel("Offset / delay (ms)")
ax.set_xlabel("Time")
ax.set_title(f"NTP offset and delay measurements - {NTP_SERVER}")

ax.axhline(0, linestyle="--")
status_indicator = ax.text(
    0.01,
    0.98,
    "Waiting for NTP sample…",
    transform=ax.transAxes,
    ha="left",
    va="top",
    color="white",
    bbox={"boxstyle": "round,pad=0.35", "facecolor": "#666666", "edgecolor": "none"},
)

fig.autofmt_xdate()
fig.subplots_adjust(bottom=0.14)

segments = []

current_date = datetime.now().date()
current_segment = None
previous_time = None
running = True
was_connected = None
previous_wall_time = None
previous_monotonic_time = None
offset_times = []
offset_values = []
delay_times = []
delay_values = []
delay_line, = ax.plot(
    [], [], color="#e67e22", marker=".", markersize=4,
    label="Approx. command duration",
    zorder=1,
)
rtt_times = []
rtt_values = []
sample_monotonic_times = []
rtt_line, = ax.plot(
    [], [], color="#008b8b", marker=".", markersize=4,
    label="Packet-level UDP NTP RTT",
    zorder=1,
)
offset_clip_up, = ax.plot([], [], linestyle="none", marker="^", color="#1f77b4", markersize=7)
offset_clip_down, = ax.plot([], [], linestyle="none", marker="v", color="#1f77b4", markersize=7)
delay_clip_up, = ax.plot([], [], linestyle="none", marker="^", color="#e67e22", markersize=7)
delay_clip_down, = ax.plot([], [], linestyle="none", marker="v", color="#e67e22", markersize=7)
rtt_clip_up, = ax.plot([], [], linestyle="none", marker="^", color="#008b8b", markersize=7)
rtt_clip_down, = ax.plot([], [], linestyle="none", marker="v", color="#008b8b", markersize=7)
category_extreme_markers = {
    (series_index, is_upper): ax.plot(
        [],
        [],
        linestyle="none",
        marker="^" if is_upper else "v",
        color="#d62728",
        markeredgecolor="white",
        markeredgewidth=1.0,
        markersize=14,
        zorder=3,
        label="_nolegend_",
    )[0]
    for series_index in range(3)
    for is_upper in (True, False)
}
clip_annotations = []
history_event_artists = []
INITIAL_CLIP_CAP_MS = 500.0
UPPER_CAP_EXTRA_HEADROOM = 1.10


HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "ntp_plot_history.jsonl",
)


def load_history():
    records = []
    try:
        history_file = open(HISTORY_FILE, "r", encoding="utf-8")
    except FileNotFoundError:
        return records

    with history_file:
        for line_number, line in enumerate(history_file, start=1):
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("record must be a JSON object")
                sample_time = datetime.fromisoformat(record["timestamp"])
                segment = record["segment"]
                if not isinstance(segment, int) or segment < 0:
                    raise ValueError("segment must be a non-negative integer")

                def load_value(name):
                    value = record[name]
                    if value is None:
                        return math.nan
                    if isinstance(value, (int, float)) and math.isfinite(value):
                        return float(value)
                    raise ValueError(f"{name} must be a finite number or null")

                events = record.get("events", [])
                if not isinstance(events, list) or any(
                    not isinstance(event, dict)
                    or not isinstance(event.get("label"), str)
                    or not isinstance(event.get("color"), str)
                    for event in events
                ):
                    raise ValueError("events must contain label and color strings")

                records.append({
                    "timestamp": sample_time,
                    "offset": load_value("offset_ms"),
                    "delay": load_value("delay_ms"),
                    "rtt": load_value("rtt_ms"),
                    "segment": segment,
                    "events": events,
                })
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid saved measurement at {HISTORY_FILE}:{line_number}: {error}"
                ) from error
    return records


def save_measurement(sample_time, offset, delay, rtt, segment, events=()):
    def save_value(value):
        return value if value is not None and math.isfinite(value) else None

    record = {
        "timestamp": sample_time.isoformat(),
        "offset_ms": save_value(offset),
        "delay_ms": save_value(delay),
        "rtt_ms": save_value(rtt),
        "segment": segment,
        "events": events,
    }
    with open(HISTORY_FILE, "a", encoding="utf-8") as history_file:
        json.dump(record, history_file, allow_nan=False)
        history_file.write("\n")


def mark_event(event_time, label, color):
    event_line = ax.axvline(event_time, color=color, alpha=0.8, linewidth=1.2)
    annotation = ax.annotate(
        label,
        xy=(event_time, 0.86),
        xycoords=("data", "axes fraction"),
        ha="left",
        va="top",
        rotation=90,
        color=color,
        fontsize=8,
    )
    history_event_artists.extend((event_line, annotation))


def mark_failure(event_time):
    failure_line = ax.axvline(
        event_time,
        color="#c62828",
        alpha=0.2,
        linewidth=1,
    )
    annotation = ax.annotate(
        "FAILED",
        xy=(event_time, 0.92),
        xycoords=("data", "axes fraction"),
        ha="center",
        va="top",
        color="white",
        fontsize=8,
        bbox={
            "boxstyle": "round,pad=0.25",
            "facecolor": "#c62828",
            "edgecolor": "none",
        },
    )
    history_event_artists.extend((failure_line, annotation))


def start_new_segment():
    global current_segment

    times = []
    offsets = []
    monotonic_times = []

    # Mark samples so a newly started segment is visible immediately, even
    # before it has enough points to form a line.
    label = "NTP offset" if not segments else "_nolegend_"
    line, = ax.plot(
        [], [], color="#1f77b4", marker=".", markersize=4, label=label,
        zorder=1,
    )

    current_segment = [times, offsets, line, monotonic_times]
    segments.append(current_segment)


def clamp_values(values, upper_cap, lower_cap=None):
    if lower_cap is None:
        lower_cap = upper_cap
    return [
        min(upper_cap, max(-lower_cap, value)) if math.isfinite(value) else math.nan
        for value in values
    ]


def triangle_center_y_for_tip(plot_time, cap_y, marker_size, points_up):
    """Place the triangle tip exactly on the cap boundary in display space."""
    x_value = mdates.date2num(plot_time)
    x_pixel, cap_pixel = ax.transData.transform((x_value, cap_y))
    half_height_pixels = marker_size * fig.dpi / 72 / 2
    center_pixel = cap_pixel - half_height_pixels if points_up else cap_pixel + half_height_pixels
    return ax.transData.inverted().transform((x_pixel, center_pixel))[1]


def merge_time_buckets(
    times, values, monotonic_times, bucket_seconds, preserve_monotonic_times=()
):
    """Return one real display sample per bucket; leave source data intact."""
    if not times:
        return [], []

    origin = monotonic_times[0]
    buckets = {}
    preserved_times = set(preserve_monotonic_times)
    for plot_time, value, sampled_at in zip(times, values, monotonic_times):
        bucket = int((sampled_at - origin) / bucket_seconds)
        buckets.setdefault(bucket, []).append((plot_time, value, sampled_at))

    merged_times = []
    merged_values = []
    for bucket in sorted(buckets):
        points = buckets[bucket]
        values_in_bucket = [point[1] for point in points]
        # Retain the latest actual sample; any failure in the bucket stays visible.
        if any(not math.isfinite(value) for value in values_in_bucket):
            selected_points = [(points[-1][0], math.nan, points[-1][2])]
        else:
            selected_points = [points[-1]]
            selected_points.extend(
                point for point in points
                if point[2] in preserved_times and point[2] != selected_points[0][2]
            )
        selected_points.sort(key=lambda point: point[2])
        for plot_time, value, _ in selected_points:
            merged_times.append(plot_time)
            merged_values.append(value)
    return merged_times, merged_values


def refresh_capped_plot():
    """Apply shared rolling caps derived from all plotted series."""
    now = time.monotonic()
    display_bucket_seconds = get_sampling_interval(now - start_time)
    sample_indices = range(len(sample_monotonic_times))

    raw_series = (offset_values, delay_values, rtt_values)
    positive_percentiles = []
    negative_percentiles = []
    for values in raw_series:
        finite_values = [
            values[i]
            for i in sample_indices
            if i < len(values) and math.isfinite(values[i])
        ]
        for series_values, percentiles in (
            (sorted(max(value, 0.0) for value in finite_values), positive_percentiles),
            (sorted(max(-value, 0.0) for value in finite_values), negative_percentiles),
        ):
            if series_values:
                percentile_position = (CAP_PERCENTILE / 100) * (len(series_values) - 1)
                lower_index = int(percentile_position)
                upper_index = min(lower_index + 1, len(series_values) - 1)
                fraction = percentile_position - lower_index
                percentiles.append(
                    series_values[lower_index]
                    + fraction * (series_values[upper_index] - series_values[lower_index])
                )

    if positive_percentiles or negative_percentiles:
        upper_base_cap = max(1.0, max(positive_percentiles, default=0.0) * CAP_HEADROOM_MULTIPLIER)
        lower_cap = max(1.0, max(negative_percentiles, default=0.0) * CAP_HEADROOM_MULTIPLIER)
    else:
        upper_base_cap = INITIAL_CLIP_CAP_MS
        lower_cap = INITIAL_CLIP_CAP_MS

    upper_cap = upper_base_cap * UPPER_CAP_EXTRA_HEADROOM
    series_caps = [(upper_cap, lower_cap)] * 3
    cap_config_status.set_text(f"Caps: +{upper_cap:.0f} / −{lower_cap:.0f} ms")

    preserve_monotonic_times = set()
    for values, (upper_series_cap, lower_series_cap) in zip(raw_series, series_caps):
        clipped_by_direction = ([], [])
        for value, sampled_at in zip(values, sample_monotonic_times):
            if not math.isfinite(value):
                continue
            if value > upper_series_cap:
                clipped_by_direction[0].append((value, sampled_at))
            elif value < -lower_series_cap:
                clipped_by_direction[1].append((value, sampled_at))
        if clipped_by_direction[0]:
            preserve_monotonic_times.add(
                max(clipped_by_direction[0], key=lambda point: point[0])[1]
            )
        if clipped_by_direction[1]:
            preserve_monotonic_times.add(
                min(clipped_by_direction[1], key=lambda point: point[0])[1]
            )

    # Draw every line using clipped copies; the raw series remain untouched.
    display_offset_times = []
    display_offset_values = []
    for times, values, line, monotonic_times in segments:
        merged_times, merged_values = merge_time_buckets(
            times, values, monotonic_times, display_bucket_seconds,
            preserve_monotonic_times,
        )
        line.set_data(
            merged_times,
            clamp_values(merged_values, *series_caps[0]),
        )
        display_offset_times.extend(merged_times)
        display_offset_values.extend(merged_values)
    merged_delay_times, merged_delay_values = merge_time_buckets(
        delay_times, delay_values, sample_monotonic_times, display_bucket_seconds,
        preserve_monotonic_times,
    )
    delay_line.set_data(
        merged_delay_times,
        clamp_values(merged_delay_values, *series_caps[1]),
    )
    merged_rtt_times, merged_rtt_values = merge_time_buckets(
        rtt_times, rtt_values, sample_monotonic_times, display_bucket_seconds,
        preserve_monotonic_times,
    )
    rtt_line.set_data(
        merged_rtt_times,
        clamp_values(merged_rtt_values, *series_caps[2]),
    )

    series_for_markers = (
        (offset_times, offset_values, offset_clip_up, offset_clip_down),
        (delay_times, delay_values, delay_clip_up, delay_clip_down),
        (rtt_times, rtt_values, rtt_clip_up, rtt_clip_down),
    )
    visible_values = []
    clipped_points = []
    for series_index, (times, values, upper_markers, lower_markers) in enumerate(series_for_markers):
        upper_cap, lower_cap = series_caps[series_index]
        upper_times = []
        upper_marker_y = []
        lower_times = []
        lower_marker_y = []
        for plot_time, value in zip(times, values):
            if not math.isfinite(value):
                continue
            if value > upper_cap:
                upper_times.append(plot_time)
                upper_marker_y.append(
                    triangle_center_y_for_tip(
                        plot_time, upper_cap, upper_markers.get_markersize(), True
                    )
                )
            elif value < -lower_cap:
                lower_times.append(plot_time)
                lower_marker_y.append(
                    triangle_center_y_for_tip(
                        plot_time, -lower_cap, lower_markers.get_markersize(), False
                    )
                )
        upper_markers.set_data(upper_times, upper_marker_y)
        lower_markers.set_data(lower_times, lower_marker_y)
        visible_values.extend(clamp_values(values, upper_cap, lower_cap))
        clipped_points.extend(
            (value, sampled_at, series_index, sample_index)
            for sample_index, (sampled_at, value) in enumerate(zip(times, values))
            if math.isfinite(value)
            and (value > upper_cap or value < -lower_cap)
        )

    finite_visible = [value for value in visible_values if math.isfinite(value)]
    lower = min([0.0] + finite_visible)
    top = upper_cap * 1.08
    bottom = lower - max(5.0, (top - lower) * 0.04)
    ax.relim()
    ax.autoscale_view(scalex=True, scaley=False)
    ax.set_ylim(bottom, top)

    category_extremes = {}
    for point in clipped_points:
        value, _, series_index, _ = point
        is_upper = value > 0
        key = (series_index, is_upper)
        previous = category_extremes.get(key)
        if previous is None or (value > previous[0] if is_upper else value < previous[0]):
            category_extremes[key] = point

    for annotation in clip_annotations:
        annotation.remove()
    clip_annotations.clear()

    # Label each contiguous cluster of clipped points with its peak actual
    # value. Center cluster labels between triangles; offset a lone peak below
    # its triangle (above it for a lower-bound clip).
    label_candidates = []
    cluster_centers = {}
    for series_index, (times, values, color) in enumerate((
        (offset_times, offset_values, "#1f77b4"),
        (delay_times, delay_values, "#e67e22"),
        (rtt_times, rtt_values, "#008b8b"),
    )):
        upper_cap, lower_cap = series_caps[series_index]
        for is_upper in (True, False):
            clipped_indices = [
                i for i, value in enumerate(values)
                if math.isfinite(value) and (value > upper_cap if is_upper else value < -lower_cap)
            ]
            groups = []
            for index in clipped_indices:
                if not groups or index > groups[-1][-1] + 1:
                    groups.append([index])
                else:
                    groups[-1].append(index)

            for group in groups:
                peak_index = max(group, key=lambda i: values[i]) if is_upper else min(group, key=lambda i: values[i])
                label_time = times[group[0]] + (times[group[-1]] - times[group[0]]) / 2
                label = f"{values[peak_index]:,.0f}"
                peak_value = values[peak_index]
                label_candidates.append((
                    label_time, is_upper, color, label, peak_value,
                    series_index, tuple(group),
                ))
                for index in group:
                    cluster_centers[(series_index, index)] = label_time

    label_rows = {True: [], False: []}

    for (series_index, is_upper), marker in category_extreme_markers.items():
        extreme = category_extremes.get((series_index, is_upper))
        if extreme is None:
            marker.set_data([], [])
            continue

        value, sample_time, _, sample_index = extreme
        upper_cap, lower_cap = series_caps[series_index]
        cap_y = upper_cap if is_upper else -lower_cap
        marker_y = triangle_center_y_for_tip(
            sample_time, cap_y, marker.get_markersize(), is_upper
        )
        marker.set_data(
            [sample_time],
            [marker_y],
        )
        label_time = cluster_centers.get((series_index, sample_index), sample_time)
        if not any(
            candidate[5] == series_index and sample_index in candidate[6]
            and candidate[1] == is_upper
            for candidate in label_candidates
        ):
            label_candidates.append((
                label_time, is_upper, "#d62728",
                f"{value:,.0f}", value,
                series_index, (sample_index,),
            ))

    legend_bbox = None
    legend = ax.get_legend()
    if legend is not None:
        try:
            legend_bbox = legend.get_window_extent(fig.canvas.get_renderer())
        except RuntimeError:
            pass

    for label_time, is_upper, color, label, peak_value, series_index, group in sorted(
        label_candidates, key=lambda item: mdates.date2num(item[0])
    ):
        upper_cap, lower_cap = series_caps[series_index]
        is_extreme = (
            (series_index, is_upper) in category_extremes
            and category_extremes[(series_index, is_upper)][3] in group
        )
        marker_clearance = 12
        x_pixel = ax.transData.transform((mdates.date2num(label_time), 0))[0]
        # Reserve extra horizontal space so labels stack before their boxes touch.
        label_half_width = max(16, len(label) * 5)
        row = 0
        while True:
            while row == len(label_rows[is_upper]):
                label_rows[is_upper].append(float("-inf"))
            has_horizontal_room = (
                x_pixel - label_half_width > label_rows[is_upper][row] + 12
            )
            overlaps_legend = False
            if legend_bbox is not None:
                anchor_y = ax.transData.transform((mdates.date2num(label_time), upper_cap if is_upper else -lower_cap))[1]
                offset_y = (
                    (-12 - marker_clearance - row * 12)
                    if is_upper else (10 + marker_clearance + row * 12)
                )
                label_y = anchor_y + offset_y * fig.dpi / 72
                label_height = 16
                label_box = (
                    x_pixel - label_half_width,
                    label_y - label_height if is_upper else label_y,
                    x_pixel + label_half_width,
                    label_y if is_upper else label_y + label_height,
                )
                overlaps_legend = not (
                    label_box[2] < legend_bbox.x0
                    or label_box[0] > legend_bbox.x1
                    or label_box[3] < legend_bbox.y0
                    or label_box[1] > legend_bbox.y1
                )
            if has_horizontal_room and not overlaps_legend:
                break
            row += 1
        label_rows[is_upper][row] = x_pixel + label_half_width

        annotation = ax.annotate(
            label,
            xy=(label_time, upper_cap if is_upper else -lower_cap),
            xytext=(
                0,
                (-12 - marker_clearance - row * 12)
                if is_upper else (10 + marker_clearance + row * 12),
            ),
            textcoords="offset points",
            ha="center",
            va="top" if is_upper else "bottom",
            color=(
                "#d62728"
                if is_extreme else color
            ),
            fontsize=8,
            fontweight="bold",
            bbox={
                "boxstyle": "round,pad=0.15",
                "facecolor": "white",
                "edgecolor": "none",
                "alpha": 1.0,
            },
            zorder=21 if is_extreme else 20,
        )
        clip_annotations.append(annotation)

def on_close(event):
    global running
    running = False


fig.canvas.mpl_connect("close_event", on_close)


def adapt_y_ticks(event=None):
    # Allow roughly one labeled interval per 55 screen pixels of plot height.
    plot_height = ax.get_window_extent().height
    tick_intervals = max(5, int(plot_height / 55))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=tick_intervals))
    if sample_monotonic_times:
        refresh_capped_plot()
    fig.canvas.draw_idle()


fig.canvas.mpl_connect("resize_event", adapt_y_ticks)
adapt_y_ticks()

percentile_text = TextBox(
    fig.add_axes([0.05, 0.05, 0.10, 0.05]),
    "",
    initial=str(CAP_PERCENTILE),
)
headroom_text = TextBox(
    fig.add_axes([0.27, 0.05, 0.10, 0.05]),
    "",
    initial=str(CAP_HEADROOM_MULTIPLIER),
)
apply_cap_button = Button(fig.add_axes([0.46, 0.05, 0.08, 0.05]), "Apply")
reset_history_button = Button(fig.add_axes([0.56, 0.05, 0.10, 0.05]), "Reset history")
percentile_label = fig.text(0, 0, "Percentile (%)", ha="right", va="center", fontsize=9)
headroom_label = fig.text(0, 0, "Headroom ×", ha="right", va="center", fontsize=9)
cap_config_status = fig.text(
    0.57,
    0.075,
    f"Cap: {CAP_PERCENTILE:g}% × {CAP_HEADROOM_MULTIPLIER:g}",
    ha="left",
    va="center",
    fontsize=8,
)


def layout_cap_controls(event=None):
    """Keep widget boxes at fixed pixel dimensions as the window is resized."""
    width_px, height_px = fig.canvas.get_width_height()
    percentile_width, headroom_width = 100, 100
    button_width, reset_width, status_width = 70, 100, 190
    control_height, bottom_px = 34, 18
    total_width = (
        88 + 8 + percentile_width + 18 + 72 + 8 + headroom_width + 14
        + button_width + 14 + reset_width + 14 + status_width
    )
    x_px = max(8, (width_px - total_width) / 2)

    center_y = (bottom_px + control_height / 2) / height_px
    percentile_label.set_position(((x_px + 88) / width_px, center_y))
    x_px += 96
    percentile_text.ax.set_position([x_px / width_px, bottom_px / height_px, percentile_width / width_px, control_height / height_px])
    x_px += percentile_width + 18
    headroom_label.set_position(((x_px + 72) / width_px, center_y))
    x_px += 80
    headroom_text.ax.set_position([x_px / width_px, bottom_px / height_px, headroom_width / width_px, control_height / height_px])
    x_px += headroom_width + 14
    apply_cap_button.ax.set_position([x_px / width_px, bottom_px / height_px, button_width / width_px, control_height / height_px])
    x_px += button_width + 14
    cap_config_status.set_position((x_px / width_px, (bottom_px + control_height / 2) / height_px))
    x_px += status_width + 14
    reset_history_button.ax.set_position([x_px / width_px, bottom_px / height_px, reset_width / width_px, control_height / height_px])


def mark_field_validity(text_box, valid):
    color = "#888888" if valid else "#c62828"
    for spine in text_box.ax.spines.values():
        spine.set_edgecolor(color)
        spine.set_linewidth(1.0 if valid else 2.0)
    label = percentile_label if text_box is percentile_text else headroom_label
    label.set_color("#333333" if valid else "#c62828")


def apply_cap_settings(event=None):
    global CAP_PERCENTILE, CAP_HEADROOM_MULTIPLIER
    percentile = None
    headroom = None
    percentile_valid = True
    headroom_valid = True
    errors = []
    try:
        percentile = float(percentile_text.text)
        percentile_valid = math.isfinite(percentile) and 0 < percentile <= 100
    except ValueError:
        percentile_valid = False
    if not percentile_valid:
        errors.append("percentile must be in (0, 100]")

    try:
        headroom = float(headroom_text.text)
        headroom_valid = math.isfinite(headroom) and headroom > 0
    except ValueError:
        headroom_valid = False
    if not headroom_valid:
        errors.append("headroom must be greater than 0")

    mark_field_validity(percentile_text, percentile_valid)
    mark_field_validity(headroom_text, headroom_valid)
    if errors:
        cap_config_status.set_text("Invalid input — check red field(s)")
        cap_config_status.set_color("#c62828")
        fig.canvas.draw_idle()
        return

    CAP_PERCENTILE = percentile
    CAP_HEADROOM_MULTIPLIER = headroom
    cap_config_status.set_text(f"Cap: {CAP_PERCENTILE:g}% × {CAP_HEADROOM_MULTIPLIER:g}")
    cap_config_status.set_color("#333333")
    if sample_monotonic_times:
        refresh_capped_plot()
    fig.canvas.draw_idle()


def reset_history(event=None):
    global current_date, current_segment, previous_time
    global was_connected, previous_wall_time, previous_monotonic_time, start_time

    try:
        os.remove(HISTORY_FILE)
    except FileNotFoundError:
        pass
    except OSError as error:
        message = f"History reset failed: {error}"
        print(message)
        status_indicator.set_text("History reset failed")
        status_indicator.set_bbox({
            "boxstyle": "round,pad=0.35",
            "facecolor": "#c62828",
            "edgecolor": "none",
        })
        fig.canvas.draw_idle()
        return

    for event_artist in history_event_artists:
        event_artist.remove()
    history_event_artists.clear()
    for _, _, line, _ in segments:
        line.remove()
    segments.clear()

    offset_times.clear()
    offset_values.clear()
    delay_times.clear()
    delay_values.clear()
    rtt_times.clear()
    rtt_values.clear()
    sample_monotonic_times.clear()
    delay_line.set_data([], [])
    rtt_line.set_data([], [])

    current_date = datetime.now().date()
    current_segment = None
    previous_time = None
    was_connected = None
    previous_wall_time = None
    previous_monotonic_time = None
    start_time = time.monotonic()
    start_new_segment()
    ax.legend(loc="upper right", framealpha=0.8, facecolor="white", edgecolor="#d0d0d0")

    status_indicator.set_text("History reset")
    status_indicator.set_bbox({
        "boxstyle": "round,pad=0.35",
        "facecolor": "#666666",
        "edgecolor": "none",
    })
    refresh_capped_plot()
    fig.canvas.draw_idle()


apply_cap_button.on_clicked(apply_cap_settings)
reset_history_button.on_clicked(reset_history)
percentile_text.on_submit(apply_cap_settings)
headroom_text.on_submit(apply_cap_settings)
layout_cap_controls()
fig.canvas.mpl_connect("resize_event", layout_cap_controls)

start_time = time.monotonic()

history = load_history()
if history:
    history_elapsed = [0.0]
    for previous, current in zip(history, history[1:]):
        wall_delta = (current["timestamp"] - previous["timestamp"]).total_seconds()
        history_elapsed.append(history_elapsed[-1] + max(0.001, wall_delta))
    history_monotonic_start = time.monotonic() - history_elapsed[-1]

    previous_segment = None
    for record, elapsed in zip(history, history_elapsed):
        if record["segment"] != previous_segment:
            start_new_segment()
            previous_segment = record["segment"]
        plot_time = record["timestamp"]
        current_segment[0].append(plot_time)
        current_segment[1].append(record["offset"])
        current_segment[3].append(history_monotonic_start + elapsed)
        offset_times.append(plot_time)
        offset_values.append(record["offset"])
        delay_times.append(plot_time)
        delay_values.append(record["delay"])
        rtt_times.append(plot_time)
        rtt_values.append(record["rtt"])
        sample_monotonic_times.append(history_monotonic_start + elapsed)

        if math.isfinite(record["offset"]):
            for event in record["events"]:
                mark_event(plot_time, event["label"], event["color"])
        else:
            mark_failure(plot_time)

    latest_success = next(
        (record for record in reversed(history) if math.isfinite(record["offset"])),
        None,
    )
    current_date = history[-1]["timestamp"].date()
    if latest_success is not None:
        previous_time = datetime.strptime(
            latest_success["timestamp"].strftime("%H:%M:%S"), "%H:%M:%S"
        )
    was_connected = math.isfinite(history[-1]["offset"])
    if was_connected:
        status_indicator.set_text("Connected")
        status_indicator.set_bbox({
            "boxstyle": "round,pad=0.35",
            "facecolor": "#2e7d32",
            "edgecolor": "none",
        })
    else:
        status_indicator.set_text("Connection failed")
        status_indicator.set_bbox({
            "boxstyle": "round,pad=0.35",
            "facecolor": "#c62828",
            "edgecolor": "none",
        })

    print(f"Loaded {len(history)} saved measurements from {HISTORY_FILE}")
else:
    start_new_segment()

ax.plot([], [], linestyle="none", marker="^", color="#555555", label="Value clipped at cap")
ax.plot([], [], linestyle="none", marker="^", color="#d62728", markersize=12, label="Clipped extremes")
ax.legend(loc="upper right", framealpha=0.8, facecolor="white", edgecolor="#d0d0d0")

if history:
    refresh_capped_plot()

plt.show(block=False)


while running:

    # --------------------------------------------------------------
    # Take the sample.
    # --------------------------------------------------------------

    sample_start = time.monotonic()

    sample_time, offset, error = get_ntp_sample(NTP_SERVER)

    sample_duration = time.monotonic() - sample_start
    packet_rtt_ms, packet_rtt_error = get_packet_rtt(NTP_SERVER)
    if packet_rtt_ms is not None:
        packet_rtt_summary = f"packet RTT {packet_rtt_ms:.1f} ms"
    else:
        packet_rtt_summary = f"packet RTT unavailable: {packet_rtt_error}"
    wall_end = datetime.now()
    monotonic_end = time.monotonic()

    # --------------------------------------------------------------
    # Process successful sample.
    # --------------------------------------------------------------

    if sample_time is not None:
        sample_events = []

        status_indicator.set_text("Connected")
        status_indicator.set_bbox({
            "boxstyle": "round,pad=0.35",
            "facecolor": "#2e7d32",
            "edgecolor": "none",
        })

        # A time-of-day decrease can mean either midnight or a manual/system
        # clock adjustment. Only treat a near-midnight rollover as a new day.
        if previous_time is not None and sample_time < previous_time:
            crossed_midnight = (
                previous_time.hour >= 23
                and sample_time.hour <= 1
            )
            if crossed_midnight:
                current_date += timedelta(days=1)
            # Start a fresh line for either kind of discontinuity. For a clock
            # rollback, keep the date so the new samples plot at their actual
            # earlier wall-clock times instead of being pushed into tomorrow.
            start_new_segment()

        previous_time = sample_time

        plot_time = datetime.combine(
            current_date,
            sample_time.time()
        )

        if was_connected is False:
            mark_event(plot_time, "Reconnected", "#2e7d32")
            sample_events.append({"label": "Reconnected", "color": "#2e7d32"})

        if previous_wall_time is not None:
            wall_delta = (wall_end - previous_wall_time).total_seconds()
            monotonic_delta = monotonic_end - previous_monotonic_time
            adjustment = wall_delta - monotonic_delta
            if abs(adjustment) >= 2.0:
                event_label = f"System clock adjusted ({adjustment:+.1f}s)"
                mark_event(
                    plot_time,
                    event_label,
                    "#1565c0",
                )
                sample_events.append({"label": event_label, "color": "#1565c0"})

        was_connected = True

        current_segment[0].append(plot_time)
        current_segment[1].append(offset)
        current_segment[3].append(monotonic_end)
        offset_times.append(plot_time)
        offset_values.append(offset)
        delay_times.append(plot_time)
        delay_values.append(sample_duration * 1000)
        rtt_times.append(plot_time)
        rtt_values.append(packet_rtt_ms if packet_rtt_ms is not None else math.nan)
        sample_monotonic_times.append(monotonic_end)
        save_measurement(
            plot_time,
            offset,
            sample_duration * 1000,
            packet_rtt_ms,
            len(segments) - 1,
            sample_events,
        )
        refresh_capped_plot()

        fig.canvas.draw_idle()
        fig.canvas.flush_events()

        print(
            f"{plot_time:%Y-%m-%d %H:%M:%S}, "
            f"{offset:+.3f} ms, approximate command duration {sample_duration * 1000:.1f} ms, "
            f"{packet_rtt_summary}"
        )

    # --------------------------------------------------------------
    # Process failed sample.
    # --------------------------------------------------------------

    else:

        failure_time = datetime.now()
        was_connected = False

        status_indicator.set_text("Connection failed")
        status_indicator.set_bbox({
            "boxstyle": "round,pad=0.35",
            "facecolor": "#c62828",
            "edgecolor": "none",
        })

        # NaN breaks the line at this sample, making the outage visible.
        current_segment[0].append(failure_time)
        current_segment[1].append(math.nan)
        current_segment[3].append(monotonic_end)
        offset_times.append(failure_time)
        offset_values.append(math.nan)

        mark_failure(failure_time)

        delay_times.append(failure_time)
        delay_values.append(sample_duration * 1000)
        rtt_times.append(failure_time)
        rtt_values.append(packet_rtt_ms if packet_rtt_ms is not None else math.nan)
        sample_monotonic_times.append(monotonic_end)
        save_measurement(
            failure_time,
            None,
            sample_duration * 1000,
            packet_rtt_ms,
            len(segments) - 1,
        )
        refresh_capped_plot()

        fig.canvas.draw_idle()
        fig.canvas.flush_events()

        print(
            f"{failure_time:%Y-%m-%d %H:%M:%S}, "
            f"NTP FAILED: {error} "
            f"(approx. command duration {sample_duration * 1000:.1f} ms, "
            f"{packet_rtt_summary})"
        )

    previous_wall_time = wall_end
    previous_monotonic_time = monotonic_end

    # --------------------------------------------------------------
    # Determine the interval for the NEXT sample.
    # --------------------------------------------------------------

    elapsed = time.monotonic() - start_time
    interval = get_sampling_interval(elapsed)

    print(
        f"Next sample in {interval}s "
        f"(elapsed {elapsed / 60:.1f} min)"
    )

    # --------------------------------------------------------------
    # Wait the full interval AFTER the sample.
    # --------------------------------------------------------------

    end_time = time.monotonic() + interval

    while running and time.monotonic() < end_time:
        fig.canvas.flush_events()
        time.sleep(0.2)


plt.close(fig)
