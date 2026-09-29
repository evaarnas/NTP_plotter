import subprocess
import re
import time
import math
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import matplotlib.dates as mdates


NTP_SERVER = "time.windows.com"

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


def get_ntp_sample():
    """Return (sample_time, offset_ms, error)."""

    try:
        result = subprocess.run(
            [
                "w32tm",
                "/stripchart",
                f"/computer:{NTP_SERVER}",
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


plt.ion()

fig, ax = plt.subplots()

ax.xaxis.set_major_formatter(
    mdates.DateFormatter("%H:%M:%S")
)

ax.set_ylabel("NTP offset (ms)")
ax.set_xlabel("Time")
ax.set_title(f"NTP clock offset - {NTP_SERVER}")

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

segments = []

current_date = datetime.now().date()
current_segment = None
previous_time = None
running = True


def start_new_segment():
    global current_segment

    times = []
    offsets = []

    line, = ax.plot([], [])

    current_segment = [times, offsets, line]
    segments.append(current_segment)


def on_close(event):
    global running
    running = False


fig.canvas.mpl_connect("close_event", on_close)

plt.show(block=False)

start_time = time.monotonic()

start_new_segment()


while running:

    # --------------------------------------------------------------
    # Take the sample.
    # --------------------------------------------------------------

    sample_start = time.monotonic()

    sample_time, offset, error = get_ntp_sample()

    sample_duration = time.monotonic() - sample_start

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

        # Detect midnight.
        if (
            previous_time is not None
            and sample_time < previous_time
        ):
            current_date += timedelta(days=1)
            start_new_segment()

        previous_time = sample_time

        plot_time = datetime.combine(
            current_date,
            sample_time.time()
        )

        current_segment[0].append(plot_time)
        current_segment[1].append(offset)

        current_segment[2].set_data(
            current_segment[0],
            current_segment[1]
        )

        ax.relim()
        ax.autoscale_view()

        fig.canvas.draw_idle()
        fig.canvas.flush_events()

        print(
            f"{plot_time:%Y-%m-%d %H:%M:%S}, "
            f"{offset:+.3f} ms "
            f"(sample took {sample_duration:.3f}s)"
        )

    # --------------------------------------------------------------
    # Process failed sample.
    # --------------------------------------------------------------

    else:

        failure_time = datetime.now()

        status_indicator.set_text("Connection failed")
        status_indicator.set_bbox({
            "boxstyle": "round,pad=0.35",
            "facecolor": "#c62828",
            "edgecolor": "none",
        })

        # NaN breaks the line at this sample, making the outage visible.
        current_segment[0].append(failure_time)
        current_segment[1].append(math.nan)

        current_segment[2].set_data(
            current_segment[0],
            current_segment[1]
        )

        ax.relim()
        ax.autoscale_view()

        fig.canvas.draw_idle()
        fig.canvas.flush_events()

        print(
            f"{failure_time:%Y-%m-%d %H:%M:%S}, "
            f"NTP FAILED: {error} "
            f"(sample took {sample_duration:.3f}s)"
        )

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
