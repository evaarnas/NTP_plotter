import subprocess
import re
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
)
rtt_times = []
rtt_values = []
sample_monotonic_times = []
rtt_line, = ax.plot(
    [], [], color="#008b8b", marker=".", markersize=4,
    label="Packet-level UDP NTP RTT",
)
offset_clip_up, = ax.plot([], [], linestyle="none", marker="^", color="#1f77b4", markersize=7)
offset_clip_down, = ax.plot([], [], linestyle="none", marker="v", color="#1f77b4", markersize=7)
delay_clip_up, = ax.plot([], [], linestyle="none", marker="^", color="#e67e22", markersize=7)
delay_clip_down, = ax.plot([], [], linestyle="none", marker="v", color="#e67e22", markersize=7)
rtt_clip_up, = ax.plot([], [], linestyle="none", marker="^", color="#008b8b", markersize=7)
rtt_clip_down, = ax.plot([], [], linestyle="none", marker="v", color="#008b8b", markersize=7)
highest_clip_marker, = ax.plot(
    [], [], linestyle="none", marker="^", color="#d62728",
    markeredgecolor="white", markeredgewidth=1.0, markersize=16,
    zorder=3, label="_nolegend_",
)
smallest_clip_marker, = ax.plot(
    [], [], linestyle="none", marker="v", color="#d62728",
    markeredgecolor="white", markeredgewidth=0.8, markersize=12,
    zorder=3, label="_nolegend_",
)
clip_annotations = []
CLIP_WINDOW_SECONDS = 5 * 60
INITIAL_CLIP_CAP_MS = 500.0
UPPER_CAP_EXTRA_HEADROOM = 1.25


def mark_event(event_time, label, color):
    ax.axvline(event_time, color=color, alpha=0.8, linewidth=1.2)
    ax.annotate(
        label,
        xy=(event_time, 0.86),
        xycoords=("data", "axes fraction"),
        ha="left",
        va="top",
        rotation=90,
        color=color,
        fontsize=8,
    )


def start_new_segment():
    global current_segment

    times = []
    offsets = []
    monotonic_times = []

    # Mark samples so a newly started segment is visible immediately, even
    # before it has enough points to form a line.
    label = "NTP offset" if not segments else "_nolegend_"
    line, = ax.plot([], [], color="#1f77b4", marker=".", markersize=4, label=label)

    current_segment = [times, offsets, line, monotonic_times]
    segments.append(current_segment)


def clamp_values(values, upper_cap, lower_cap=None):
    if lower_cap is None:
        lower_cap = upper_cap
    return [
        min(upper_cap, max(-lower_cap, value)) if math.isfinite(value) else math.nan
        for value in values
    ]


def merge_time_buckets(times, values, monotonic_times, bucket_seconds):
    """Return one real display sample per bucket; leave source data intact."""
    if not times:
        return [], []

    origin = monotonic_times[0]
    buckets = {}
    for plot_time, value, sampled_at in zip(times, values, monotonic_times):
        bucket = int((sampled_at - origin) / bucket_seconds)
        buckets.setdefault(bucket, []).append((plot_time, value))

    merged_times = []
    merged_values = []
    for bucket in sorted(buckets):
        points = buckets[bucket]
        values_in_bucket = [point[1] for point in points]
        # Retain the latest actual sample; any failure in the bucket stays visible.
        if any(not math.isfinite(value) for value in values_in_bucket):
            merged_times.append(points[-1][0])
            merged_value = math.nan
        else:
            merged_times.append(points[-1][0])
            merged_value = points[-1][1]
        merged_values.append(merged_value)
    return merged_times, merged_values


def refresh_capped_plot():
    """Apply one latency-based rolling cap to all three series."""
    now = time.monotonic()
    display_bucket_seconds = get_sampling_interval(now - start_time)
    recent_indices = [
        i for i, sampled_at in enumerate(sample_monotonic_times)
        if now - sampled_at <= CLIP_WINDOW_SECONDS
    ]

    recent_latency_values = []
    if recent_indices:
        for i in recent_indices:
            # Keep clock-offset steps from expanding the cap until the
            # excursions themselves stop being clipped. Latency series set
            # the readable scale; all three plotted series still share it.
            for series in (delay_values, rtt_values):
                value = series[i]
                if math.isfinite(value):
                    recent_latency_values.append(abs(value))

    if not recent_latency_values:
        cap = INITIAL_CLIP_CAP_MS
    else:
        recent_latency_values.sort()
        index = int((CAP_PERCENTILE / 100) * (len(recent_latency_values) - 1))
        cap = max(1.0, recent_latency_values[index] * CAP_HEADROOM_MULTIPLIER)

    lower_cap = cap
    upper_cap = cap * UPPER_CAP_EXTRA_HEADROOM

    cap_config_status.set_text(
        f"Caps: +{upper_cap:.0f} / −{lower_cap:.0f} ms"
    )

    # Draw every line using clipped copies; the raw series remain untouched.
    display_offset_times = []
    display_offset_values = []
    for times, values, line, monotonic_times in segments:
        merged_times, merged_values = merge_time_buckets(
            times, values, monotonic_times, display_bucket_seconds
        )
        line.set_data(merged_times, clamp_values(merged_values, upper_cap, lower_cap))
        display_offset_times.extend(merged_times)
        display_offset_values.extend(merged_values)
    merged_delay_times, merged_delay_values = merge_time_buckets(
        delay_times, delay_values, sample_monotonic_times, display_bucket_seconds
    )
    delay_line.set_data(merged_delay_times, clamp_values(merged_delay_values, upper_cap, lower_cap))
    merged_rtt_times, merged_rtt_values = merge_time_buckets(
        rtt_times, rtt_values, sample_monotonic_times, display_bucket_seconds
    )
    rtt_line.set_data(merged_rtt_times, clamp_values(merged_rtt_values, upper_cap, lower_cap))

    series_for_markers = (
        (display_offset_times, display_offset_values, offset_clip_up, offset_clip_down),
        (merged_delay_times, merged_delay_values, delay_clip_up, delay_clip_down),
        (merged_rtt_times, merged_rtt_values, rtt_clip_up, rtt_clip_down),
    )
    visible_values = []
    clipped_points = []
    for series_index, (times, values, upper_markers, lower_markers) in enumerate(series_for_markers):
        upper_times = [t for t, value in zip(times, values) if math.isfinite(value) and value > upper_cap]
        lower_times = [t for t, value in zip(times, values) if math.isfinite(value) and value < -lower_cap]
        upper_markers.set_data(upper_times, [upper_cap] * len(upper_times))
        lower_markers.set_data(lower_times, [-lower_cap] * len(lower_times))
        visible_values.extend(clamp_values(values, upper_cap, lower_cap))
        clipped_points.extend(
            (value, sampled_at, series_index, sample_index)
            for sample_index, (sampled_at, value) in enumerate(zip(times, values))
            if math.isfinite(value) and (value > upper_cap or value < -lower_cap)
        )

    finite_visible = [value for value in visible_values if math.isfinite(value)]
    lower = min([0.0] + finite_visible)
    top = upper_cap * 1.08
    bottom = lower - max(5.0, (top - lower) * 0.04)
    ax.relim()
    ax.autoscale_view(scalex=True, scaley=False)
    ax.set_ylim(bottom, top)

    if clipped_points:
        # "Largest" means the highest signed value; the minimum below captures
        # the lower extreme independently, so positive and negative tails both
        # receive their own marker when present.
        highest_point = max(clipped_points, key=lambda point: point[0])
        highest_value, _, highest_series, highest_index = highest_point
        highest_clip_marker.set_marker("^" if highest_value > 0 else "v")
        smallest_point = min(clipped_points, key=lambda point: point[0])
        smallest_value, smallest_time, smallest_series, smallest_index = smallest_point
        smallest_clip_marker.set_marker("^" if smallest_value > 0 else "v")
    else:
        highest_value = None
        highest_series = highest_index = None
        smallest_value = None
        smallest_time = None
        smallest_series = smallest_index = None
        highest_clip_marker.set_data([], [])
        smallest_clip_marker.set_data([], [])

    for annotation in clip_annotations:
        annotation.remove()
    clip_annotations.clear()

    # Label each contiguous cluster of clipped points with its peak actual
    # value. Center cluster labels between triangles; offset a lone peak below
    # its triangle (above it for a lower-bound clip).
    label_candidates = []
    cluster_centers = {}
    for series_index, (times, values, color) in enumerate((
        (display_offset_times, display_offset_values, "#1f77b4"),
        (merged_delay_times, merged_delay_values, "#e67e22"),
        (merged_rtt_times, merged_rtt_values, "#008b8b"),
    )):
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

    highest_time = (
        cluster_centers.get((highest_series, highest_index), highest_point[1])
        if highest_value is not None else None
    )
    highest_candidate_exists = False
    if highest_value is not None:
        highest_candidate_exists = any(
            candidate[5] == highest_series and highest_index in candidate[6]
            for candidate in label_candidates
        )
        if not highest_candidate_exists:
            label_candidates.append((
                highest_time, highest_value > 0, "#d62728",
                f"{highest_value:,.0f}", highest_value,
                highest_series, (highest_index,),
            ))
    highest_clip_marker.set_data(
        [highest_time] if highest_time is not None else [],
        [upper_cap if highest_value > 0 else -lower_cap] if highest_time is not None else [],
    )

    smallest_is_distinct = (
        smallest_value is not None
        and (smallest_series, smallest_index) != (highest_series, highest_index)
    )
    smallest_time = (
        cluster_centers.get((smallest_series, smallest_index), smallest_time)
        if smallest_value is not None else None
    )
    if smallest_is_distinct:
        smallest_candidate_exists = any(
            candidate[5] == smallest_series and smallest_index in candidate[6]
            for candidate in label_candidates
        )
        if not smallest_candidate_exists:
            label_candidates.append((
                smallest_time, smallest_value > 0, "#d62728",
                f"{smallest_value:,.0f}", smallest_value,
                smallest_series, (smallest_index,),
            ))
        smallest_times = [smallest_time]
    else:
        smallest_times = []

    smallest_clip_marker.set_data(
        smallest_times,
        [(upper_cap if smallest_value > 0 else -lower_cap)] * len(smallest_times)
        if smallest_times else [],
    )

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
                offset_y = (-12 - row * 14) if is_upper else (10 + row * 14)
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
            xytext=(0, (-12 - row * 14) if is_upper else (10 + row * 14)),
            textcoords="offset points",
            ha="center",
            va="top" if is_upper else "bottom",
            color=(
                "#d62728"
                if (
                    series_index == highest_series and highest_index in group
                ) or (
                    smallest_is_distinct
                    and series_index == smallest_series
                    and smallest_index in group
                )
                else color
            ),
            fontsize=8,
            fontweight="bold",
            bbox={
                "boxstyle": "round,pad=0.15",
                "facecolor": "white",
                "edgecolor": "none",
                "alpha": 0.95,
            },
            zorder=10,
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
    button_width, status_width = 70, 190
    control_height, bottom_px = 34, 18
    total_width = 88 + 8 + percentile_width + 18 + 72 + 8 + headroom_width + 14 + button_width + 14 + status_width
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


apply_cap_button.on_clicked(apply_cap_settings)
percentile_text.on_submit(apply_cap_settings)
headroom_text.on_submit(apply_cap_settings)
layout_cap_controls()
fig.canvas.mpl_connect("resize_event", layout_cap_controls)

plt.show(block=False)

start_time = time.monotonic()

start_new_segment()
ax.plot([], [], linestyle="none", marker="^", color="#555555", label="Value clipped at cap")
ax.plot([], [], linestyle="none", marker="^", color="#d62728", markersize=12, label="Clipped extremes")
ax.legend(loc="upper right", framealpha=0.55)


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

        if previous_wall_time is not None:
            wall_delta = (wall_end - previous_wall_time).total_seconds()
            monotonic_delta = monotonic_end - previous_monotonic_time
            adjustment = wall_delta - monotonic_delta
            if abs(adjustment) >= 2.0:
                mark_event(
                    plot_time,
                    f"System clock adjusted ({adjustment:+.1f}s)",
                    "#1565c0",
                )

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

        ax.axvline(
            failure_time,
            color="#c62828",
            alpha=0.2,
            linewidth=1,
        )
        ax.annotate(
            "FAILED",
            xy=(failure_time, 0.92),
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

        delay_times.append(failure_time)
        delay_values.append(sample_duration * 1000)
        rtt_times.append(failure_time)
        rtt_values.append(packet_rtt_ms if packet_rtt_ms is not None else math.nan)
        sample_monotonic_times.append(monotonic_end)
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
