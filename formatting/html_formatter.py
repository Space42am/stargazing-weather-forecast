"""
Render the structured report into a self-contained HTML page.

Uses Chart.js (CDN) for interactive charts. One section per location,
each with a cloud-cover chart (grouped stacked bars per model) and a
temperature/wind line chart on dual Y-axes.

Requires a browser with internet access for the Chart.js CDN.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

# Dark colour palette (Catppuccin Mocha-inspired)
_MODEL_CLOUD_COLORS = {
    "GFS":   {"low": "rgba(59,130,246,.85)",  "mid": "rgba(147,197,253,.85)",  "high": "rgba(219,234,254,.7)"},
    "ICON":  {"low": "rgba(251,146,60,.85)",  "mid": "rgba(253,186,116,.85)",  "high": "rgba(254,215,170,.7)"},
    "ECMWF": {"low": "rgba(74,222,128,.85)",  "mid": "rgba(134,239,172,.85)",  "high": "rgba(187,247,208,.7)"},
}
_MODEL_LINE_COLORS = {
    "GFS":   "#60a5fa",
    "ICON":  "#fb923c",
    "ECMWF": "#4ade80",
}
_FALLBACK_CLOUD = {"low": "rgba(180,180,180,.7)", "mid": "rgba(210,210,210,.7)", "high": "rgba(235,235,235,.5)"}
_FALLBACK_LINE  = "#a6adc8"


def _build_datasets(location_report: Dict[str, Any]):
    """
    Flatten the report into parallel arrays for Chart.js.

    Returns (labels, cloud_datasets, temp_datasets, wind_datasets, day_boundaries).
    day_boundaries is a list of x-positions (0.5, 1.5 …) between the last
    entry of one day and the first of the next — used for separator lines.
    """
    labels: List[str] = []
    flat: List[Dict[str, Any]] = []
    day_boundaries: List[float] = []
    current_date: Optional[str] = None
    idx = 0

    for day in location_report.get("days", []):
        date = day["date"][5:]
        if current_date is not None and date != current_date:
            day_boundaries.append(idx - 0.5)
        current_date = date
        for entry in day.get("entries", []):
            labels.append(f"{date} {entry['time']}")
            flat.append(entry)
            idx += 1

    all_models: List[str] = []
    for entry in flat:
        for m in entry.get("models", {}):
            if m not in all_models:
                all_models.append(m)

    cloud_datasets: List[Dict[str, Any]] = []
    for model in all_models:
        colors = _MODEL_CLOUD_COLORS.get(model, _FALLBACK_CLOUD)
        for layer in ("low", "mid", "high"):
            data = [
                (e.get("models", {}).get(model, {}) or {}).get(f"cloud_{layer}", 0) or 0
                for e in flat
            ]
            cloud_datasets.append({
                "label":           f"{model} {layer}",
                "data":            data,
                "backgroundColor": colors[layer],
                "categoryPercentage": 1.0,
                "barPercentage":   0.85,
            })

    temp_datasets: List[Dict[str, Any]] = []
    wind_datasets: List[Dict[str, Any]] = []
    for model in all_models:
        color = _MODEL_LINE_COLORS.get(model, _FALLBACK_LINE)
        temp_data = [
            (e.get("models", {}).get(model, {}) or {}).get("temp")
            for e in flat
        ]
        wind_data = [
            (e.get("models", {}).get(model, {}) or {}).get("wind")
            for e in flat
        ]
        temp_datasets.append({
            "label":           f"{model}",
            "data":            temp_data,
            "borderColor":     color,
            "backgroundColor": "transparent",
            "tension":         0.35,
            "yAxisID":         "yTemp",
            "pointRadius":     5,
            "pointHoverRadius": 7,
        })
        wind_datasets.append({
            "label":           f"{model}",
            "data":            wind_data,
            "borderColor":     color,
            "backgroundColor": "transparent",
            "tension":         0.35,
            "borderDash":      [6, 3],
            "yAxisID":         "yWind",
            "pointRadius":     4,
            "pointStyle":      "rectRot",
            "pointHoverRadius": 6,
        })

    return labels, cloud_datasets, temp_datasets, wind_datasets, day_boundaries


def _location_section(location_report: Dict[str, Any], idx: int) -> str:
    name = location_report.get("location", "Unknown")
    days = location_report.get("days", [])

    if not days:
        return (
            f'<section class="loc">'
            f'<h2>{name}</h2>'
            f'<p class="empty">No qualifying hours in forecast window.</p>'
            f'</section>'
        )

    labels, cloud_ds, temp_ds, wind_ds, boundaries = _build_datasets(location_report)

    lj  = json.dumps(labels,      ensure_ascii=False)
    cdj = json.dumps(cloud_ds,    ensure_ascii=False)
    tdj = json.dumps(temp_ds,     ensure_ascii=False)
    wdj = json.dumps(wind_ds,     ensure_ascii=False)
    bj  = json.dumps(boundaries)

    cid = f"cc{idx}"
    tid = f"tw{idx}"

    return f"""<section class="loc">
  <h2>{name}</h2>
  <div class="card">
    <p class="chart-title">Cloud Cover by Model</p>
    <p class="chart-sub">Low / Mid / High cloud cover per model, side by side — 0 % = clear sky, 100 % = fully overcast</p>
    <canvas id="{cid}"></canvas>
  </div>
  <div class="card">
    <p class="chart-title">Temperature &amp; Wind Speed</p>
    <p class="chart-sub">Solid lines = temperature (left axis) · dashed lines = wind speed (right axis)</p>
    <canvas id="{tid}"></canvas>
  </div>
</section>
<script>
(function(){{
  var L={lj}, CD={cdj}, TD={tdj}, WD={wdj}, BOUNDS={bj};
  var grid = 'rgba(255,255,255,.07)';
  var tick  = '#a6adc8';
  Chart.defaults.color = tick;

  // Custom plugin: draws vertical dashed lines between days
  var daySep = {{
    id:'daySep',
    afterDraw:function(chart){{
      if(!BOUNDS.length) return;
      var ctx=chart.ctx, ca=chart.chartArea;
      var xScale=chart.scales['x']||chart.scales['x-axis-0'];
      if(!xScale) return;
      ctx.save();
      ctx.setLineDash([5,4]);
      ctx.strokeStyle='rgba(220,50,50,.75)';
      ctx.lineWidth=1.5;
      BOUNDS.forEach(function(b){{
        var lo=Math.floor(b), hi=Math.ceil(b);
        if(lo<0||hi>=L.length) return;
        var xLo=xScale.getPixelForValue(lo);
        var xHi=xScale.getPixelForValue(hi);
        var x=(xLo+xHi)/2;
        ctx.beginPath();
        ctx.moveTo(x, ca.top);
        ctx.lineTo(x, ca.bottom);
        ctx.stroke();
      }});
      ctx.restore();
    }}
  }};

  new Chart(document.getElementById('{cid}'),{{
    type:'bar',
    data:{{labels:L, datasets:CD}},
    plugins:[daySep],
    options:{{
      responsive:true,
      plugins:{{
        legend:{{position:'top', labels:{{boxWidth:14, padding:12}}}},
        tooltip:{{mode:'index', intersect:false}},
      }},
      scales:{{
        x:{{grid:{{color:grid}}, ticks:{{maxRotation:45, minRotation:30}}}},
        y:{{
          min:0, max:100,
          grid:{{color:grid}},
          title:{{display:true, text:'Cloud cover (%)'}},
          ticks:{{callback:function(v){{return v+'%';}}}},
        }},
      }},
    }},
  }});

  new Chart(document.getElementById('{tid}'),{{
    type:'line',
    data:{{labels:L, datasets:[...TD,...WD]}},
    plugins:[daySep],
    options:{{
      responsive:true,
      plugins:{{
        legend:{{
          position:'top',
          labels:{{
            boxWidth:14, padding:12,
            generateLabels:function(chart){{
              var ds=chart.data.datasets, half=ds.length/2;
              return ds.map(function(d,i){{
                return {{
                  text:i<half ? d.label+' temp' : d.label+' wind',
                  strokeStyle:d.borderColor,
                  lineDash:d.borderDash||[],
                  fillStyle:'transparent',
                  hidden:!chart.isDatasetVisible(i),
                  datasetIndex:i,
                }};
              }});
            }},
          }},
        }},
        tooltip:{{mode:'index', intersect:false}},
      }},
      scales:{{
        x:{{grid:{{color:grid}}, ticks:{{maxRotation:45, minRotation:30}}}},
        yTemp:{{
          type:'linear', position:'left',
          grid:{{color:grid}},
          title:{{display:true, text:'Temperature (°C)'}},
          ticks:{{callback:function(v){{return v+'°C';}}}},
        }},
        yWind:{{
          type:'linear', position:'right',
          grid:{{drawOnChartArea:false}},
          title:{{display:true, text:'Wind (m/s)'}},
          ticks:{{callback:function(v){{return v+' m/s';}}}},
          min:0,
        }},
      }},
    }},
  }});
}})();
</script>"""


_LABEL_COLORS = {
    "Excellent": ("#a6e3a1", "#1e3a2f"),
    "Good":      ("#89b4fa", "#1a2840"),
    "Fair":      ("#f9e2af", "#3a3010"),
    "Poor":      ("#f38ba8", "#3a1020"),
}


def _recommendation_banner(ranked: List[Dict[str, Any]]) -> str:
    if not ranked:
        return ""
    best = ranked[0]
    label = best["label"]
    fg, bg = _LABEL_COLORS.get(label, ("#cdd6f4", "#313244"))

    rows = "".join(
        f"<tr{'style=\"opacity:.55\"' if i > 0 else ''}>"
        f"<td>{r['date']}</td>"
        f"<td>{r['location']}</td>"
        f"<td>{r['cloud']:.0f}%</td>"
        f"<td>{r['wind']:.1f} m/s</td>"
        f"<td><span class=\"badge\" style=\"background:{_LABEL_COLORS.get(r['label'],('','#313244'))[1]};color:{_LABEL_COLORS.get(r['label'],('#cdd6f4',''))[0]}\">{r['label']}</span></td>"
        f"</tr>"
        for i, r in enumerate(ranked[:5])
    )

    return f"""<div class="rec-box" style="background:{bg};border:1px solid {fg}22;">
  <div class="rec-header">
    <span class="rec-star">★</span>
    <span class="rec-title">Best night for stargazing</span>
    <span class="rec-badge" style="background:{fg};color:{bg}">{label}</span>
  </div>
  <div class="rec-best">
    <span class="rec-loc">{best['location']}</span>
    <span class="rec-sep">·</span>
    <span class="rec-date">{best['date']}</span>
    <span class="rec-sep">·</span>
    <span class="rec-detail">☁ {best['cloud']:.0f}% cloud · 💨 {best['wind']:.1f} m/s wind</span>
  </div>
  <details class="rec-all">
    <summary>All nights ranked</summary>
    <table class="rank-table">
      <thead><tr><th>Date</th><th>Location</th><th>Cloud</th><th>Wind</th><th>Rating</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </details>
</div>"""


def render_html(
    location_reports: List[Dict[str, Any]],
    header: Optional[str] = None,
    ranked_nights: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Return a complete HTML document as a string."""
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = header or f"Night-Hour Weather Report — {generated}"

    sections = "\n".join(
        _location_section(r, i) for i, r in enumerate(location_reports)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{
      background:#1e1e2e; color:#cdd6f4;
      font-family: system-ui, -apple-system, sans-serif;
      padding:2rem; max-width:1100px; margin:0 auto;
    }}
.loc {{ margin-bottom:3.5rem; }}
    .loc h2 {{
      font-size:1.15rem; color:#89b4fa;
      margin-bottom:1.25rem;
    }}
    .card {{
      background:#181825; border-radius:10px;
      padding:1.25rem 1.5rem; margin-bottom:1.25rem;
    }}
    .chart-title {{
      font-weight:600; font-size:.9rem; color:#cdd6f4;
      margin-bottom:.2rem;
    }}
    .chart-sub {{
      font-size:.75rem; color:#6c7086;
      margin-bottom:.9rem;
    }}
    canvas {{ max-height:300px; }}
    .empty {{ color:#6c7086; font-style:italic; }}
    footer {{
      margin-top:2rem; text-align:center;
      font-size:.8rem; color:#585b70;
    }}
  </style>
</head>
<body>
  {sections}
  <footer>Generated {generated}</footer>
</body>
</html>"""


def save_html(
    location_reports: List[Dict[str, Any]],
    output_path: str,
    header: Optional[str] = None,
) -> str:
    """Write the HTML report to *output_path* and return the path."""
    html = render_html(location_reports, header=header)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return output_path


def screenshot_html(html: str, width: int = 1300) -> bytes:
    """
    Render *html* in headless Chromium and return a full-page PNG as bytes.

    Waits for network idle so Chart.js CDN assets finish loading before
    the screenshot is taken.
    """
    import os
    import tempfile
    from playwright.sync_api import sync_playwright

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(html)
        tmp = fh.name

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.goto(f"file:///{tmp.replace(os.sep, '/')}")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)   # let chart animations settle
            png = page.screenshot(full_page=True)
            browser.close()
    finally:
        os.unlink(tmp)

    return png
