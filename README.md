# NTP Plotter

A small cross-platform desktop tool that samples an NTP server and plots clock offset,
query duration, and packet round-trip time over time.

![NTP Plotter showing clock offset, query duration, and packet RTT](assets/ntp-plotter.png)

## Requirements

- Python 3
- Matplotlib
- Tkinter (usually included with desktop Python installations)
- Network access to the configured NTP server:
  - UDP traffic to port 123 must be allowed for NTP offset and RTT measurements.

## Setup

Install Matplotlib from PowerShell, Terminal, or a shell:

```sh
python -m pip install matplotlib
```

On Windows, `py` can be used instead of `python`:

```powershell
py -m pip install matplotlib
```

## Run

From the project directory, run:

```sh
python NTP_plotter.py
```

On Windows, the Python launcher can be used instead:

```powershell
py .\NTP_plotter.py
```

The plot window updates as samples arrive. Close the window to stop sampling.
Measurements and connection errors are also printed to the console. The program
saves measurements in `ntp_plot_history.jsonl` next to the script and reloads
them when it starts. Click **Reset history** to clear the displayed data and
remove the saved history. Only one instance can run at a time; starting a second
one shows an error dialog and exits.

## Plot and sampling

The plot shows:

- **NTP offset** — calculated in milliseconds from the local request and
  response times and the server's receive and transmit timestamps in one NTP
  exchange. Positive values mean the server clock is ahead of the local clock.
- **Approx. NTP query duration** — elapsed time for the complete query,
  including address lookup and the UDP exchange.
- **Packet-level UDP NTP RTT** — elapsed send-to-reply time for that same
  direct NTP request, measured with a monotonic clock.
- **Connection failures and events** — failed offset samples, reconnections,
  and detected local system clock adjustments.

Sampling starts every 2 seconds and gradually slows as the persisted graph
span grows. Time spent with the app closed counts toward the graph span, so a
restart continues at the appropriate interval. Resetting history starts the
schedule over.

| Graph span | Sampling interval |
| --- | --- |
| First 10 minutes | 2 seconds |
| 10–30 minutes | 5 seconds |
| 30–60 minutes | 10 seconds |
| 1–3 hours | 30 seconds |
| 3–12 hours | 1 minute |
| 12–24 hours | 2 minutes |
| After 24 hours | 5 minutes |

Large values are clipped in the display so typical variation remains legible;
triangle markers and labels indicate clipped values. Use the **Percentile (%)**
and **Headroom ×** fields at the bottom of the window and click **Apply** to
adjust the display caps. The percentile must be greater than 0 and at most 100;
headroom must be greater than 0. This affects display scaling only, not the
underlying samples.

Time-axis dates appear when the date changes. Gaps longer than 10 minutes are
shaded and labeled with the time between saved samples; plotted lines are
broken across those gaps.

## Configure the NTP server

Edit `NTP_SERVER` near the top of `NTP_plotter.py` to use a different server.
The server is queried directly over UDP port 123 for both offset and RTT
measurements.
