Medic — live GPU and CPU dashboard
====================================

Start: double-click "Start PulseGPU.bat". The launcher may show a command
window briefly while it checks or installs the required package. It closes
after Medic starts; the dashboard runs on its own. The launcher filename stays
the same so existing shortcuts continue to work.

Use the header's POLL ADJUSTER tiles to sample every 1, 5, or 10 seconds; the
default is 1 second.

This dashboard reads NVIDIA GPU telemetry through nvidia-smi and CPU/RAM
telemetry through psutil. CPU core temperatures come directly from Core Temp's
documented shared-memory interface. Keep Core Temp open while using the
dashboard. Core temperatures are shown individually; the large CPU temperature
is the hottest core reading.

The dashboard shows GPU, CPU, RAM, and per-core temperatures, plus GPU/CPU
load history with 5-minute, 30-minute, 1-hour, and full-session ranges.
Temperature and GPU-load readings change color by range. GPU fan speed is shown
as a percent.
The CPU card shows an estimated system draw: measured GPU board power, an
approximation based on CPU load, and a 50 W allowance for other components.
This is an estimate, not a PSU sensor reading. The header includes the current
session duration.

Click RECORD to capture telemetry in the current Medic window. Click STOP
RECORDING to end that capture and create a PDF report in Downloads. The
dashboard and session clock continue running, and the button returns to RECORD
after export.
The report contains key session statistics, separate averages and peaks for
each CPU core, and time ranges when the GPU or a CPU core reached 80°C or higher.

Files
pulse_gpu_dashboard.py — dashboard
Start PulseGPU.bat — double-click launcher (existing filename)
Medic.ico — Medic app and shortcut icon
speedtest_button_cutout.png — Speedtest button artwork
requirements.txt — Python dependencies
README.txt — this guide
