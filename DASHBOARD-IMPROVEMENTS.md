# Dashboard Interactive Charts - Current State

**Status:** Implemented (frontend, dashboard server and Manager time-range queries)  
**History:** Plotly.js charts were introduced in `0a56285`. Time-range filtering, downsampling and the chart fixes below were added later.

This file describes what the history charts do **now**. For configuration and operations see [dashboard/README.md](dashboard/README.md).

---

## What Works

- Click any metric tile (CPU, RAM, uptime, bandwidth, GPU, VRAM) or volume bar on a card to open its history chart.
- **Plotly.js 1.58.5**, loaded from the CDN at a pinned version with a Subresource Integrity hash. This is the same version the retired `plotly-latest` URL used to serve, so the chart API didn't change. Zoom, pan, autoscale and reset come from the Plotly mode bar.
- **Time ranges:** "Last 24h", "Last 7d", "Last 30d", "All Available" and a custom From/To date range. The active range button is highlighted. A custom range with From after To is rejected with a message.
  - Metric charts open on the last 24 hours. Volume charts open on "All Available", because disk scans are daily.
  - The range is sent to the Manager as `from`/`to` (ISO 8601, UTC). The Manager filters rows by timestamp.
- **Bounded responses:** every history request carries `max_points` (config `chart_max_points`, default 2000) and a `limit`.
  - Managers that support it bucket-average long ranges server-side and set `downsampled`. They set `truncated` when the row `limit` cut the result.
  - If an older Manager returns more points than `max_points`, the dashboard server bucket-averages the response itself and adds `downsampled: true` and `downsampled_from: N`.
  - The chart info line then says the data was averaged (or cut at the row limit) and suggests a shorter range for full detail.
- **Exports:** PNG through Plotly's mode-bar camera button. CSV through the download button in the mode bar.

  CSV format:

  ```
  timestamp,value
  "2026-08-20T14:30:00Z",45.2
  "2026-08-20T14:35:00Z",48.1
  ```

  The file is named after the selection, for example `Triton_cpu_percent_24h_20260821T143000Z.csv` or `Triton_volume_volume1_all_...csv`.

  The CSV contains exactly what is plotted. When the range was bucket-averaged (`downsampled`), the value column is named `value_avg` and the file name gets `_avg` (for example `Triton_cpu_percent_all_avg_...csv`). When the row limit cut the range (`truncated`), the file name gets `_truncated`. For raw readings over a long range, export several shorter ranges.
- **Hover modes:** two extra mode-bar buttons switch between "closest point" and "point + time" hover.
- **Units:** the unit sent by the Manager (`%`, `s`, `Mbps`, `bytes`) picks the axis scaling, with a field-name fallback for older data. Byte values use binary units (GiB/TiB). Volume charts switch from GiB to TiB at 1 TiB. Unmeasured points (null) are plotted as gaps, never as 0.

## Request Handling

- Each chart request gets a sequence number and an `AbortController`.
  - Opening another chart or changing the range aborts the previous request.
  - A late response for an older selection is ignored, so a slow request can't overwrite the chart you're looking at.
  - The data used for CSV export is cleared when a new request starts. It is set only from the response for the current selection.
- An error message from a failed request is removed when the next request starts, so it can't linger under a later chart.
- Closing the chart aborts any request in flight.
- The dashboard server validates the system name and data type against `^[A-Za-z0-9_.-]{1,64}$`, rejects `.`/`..` path segments and malformed timestamps, and URL-encodes every path segment it forwards to the Manager.

## Files

| File | Role |
|------|------|
| `dashboard/templates/index.html` | Pinned Plotly script (SRI), range buttons (`data-range`), custom range inputs |
| `dashboard/static/js/dashboard.js` | Chart state (`chart` object), `applyRange`, `requestChart`, renderers, CSV export |
| `dashboard/app.py` | `/api/history/<system>/<data_type>/<field>` proxy: validation, `max_points`/`limit`, fallback `downsample()` |
| `dashboard/static/css/style.css` | `.chart-controls`, `.range-btn` (`.active`), `.chart-info`, `.chart-error` |

The pre-Plotly `dashboard-old.js` backup has been removed. It was not referenced anywhere.

## Possible Next Steps

- Reload the chart at full resolution when the user zooms into a downsampled range (`plotly_relayout` event).
- Per-folder history charts. Only volumes are clickable today.
- Per-user CPU/RAM history. The Manager already serves `/api/user_metrics/history/<device>/<user>`.

## References

- **Plotly.js docs:** https://plotly.com/javascript/
- **Plotly hover:** https://plotly.com/javascript/hover-text-and-formatting/
