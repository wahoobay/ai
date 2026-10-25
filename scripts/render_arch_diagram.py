"""Render the AWS architecture diagram for docs/grant_writeup.pdf.

Uses matplotlib to draw a print-quality vector diagram of the
two-AZ, multi-tier redundancy architecture described in §6 of the
grant writeup. Saves both PNG (raster, for PDF embedding) and PDF
(vector, for any future LaTeX use).

Run: python scripts/render_arch_diagram.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.patches as patches
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

# Page geometry — 10x12.5 inches at 200 DPI = 2000x2500 px, plenty for print.
FIG_W, FIG_H = 10, 12.5

# AWS-ish palette (loose interpretation; not an official AWS asset).
COL_COMPUTE = "#E8F4F8"  # pale blue
COL_COMPUTE_BD = "#1f78b4"
COL_NETWORK = "#FFF4E0"  # pale orange
COL_NETWORK_BD = "#e08a1c"
COL_DATA = "#EEFBE7"  # pale green
COL_DATA_BD = "#3c8a3a"
COL_EXTERN = "#F4ECF7"  # pale purple
COL_EXTERN_BD = "#6f3c8a"
COL_STANDBY = "#FAEAEA"  # pale red — standby instances
COL_STANDBY_BD = "#b13a3a"
COL_TEXT = "#1a1f2c"
COL_MUTED = "#5a6477"


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def box(
    ax,
    xy,
    w,
    h,
    *,
    face,
    edge,
    label,
    sublabel=None,
    font_size=9,
    sub_size=7,
    radius=0.10,
    lw=1.2,
    label_color=COL_TEXT,
):
    """Rounded rectangle with a primary label + optional secondary line."""
    x, y = xy
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad=0.02,rounding_size={radius}",
        linewidth=lw,
        edgecolor=edge,
        facecolor=face,
    )
    ax.add_patch(patch)
    if sublabel:
        ax.text(
            x + w / 2,
            y + h / 2 + 0.10,
            label,
            ha="center",
            va="center",
            fontsize=font_size,
            color=label_color,
            weight="bold",
        )
        ax.text(
            x + w / 2,
            y + h / 2 - 0.18,
            sublabel,
            ha="center",
            va="center",
            fontsize=sub_size,
            color=COL_MUTED,
            style="italic",
        )
    else:
        ax.text(
            x + w / 2,
            y + h / 2,
            label,
            ha="center",
            va="center",
            fontsize=font_size,
            color=label_color,
            weight="bold",
        )


def arrow(
    ax,
    src,
    dst,
    *,
    color=COL_MUTED,
    lw=1.0,
    style="-|>",
    text=None,
    curvature=0.0,
    text_offset=(0, 0.05),
):
    """Draw a directed arrow with optional label at the midpoint."""
    a = FancyArrowPatch(
        src,
        dst,
        connectionstyle=f"arc3,rad={curvature}",
        arrowstyle=style,
        mutation_scale=12,
        linewidth=lw,
        color=color,
    )
    ax.add_patch(a)
    if text:
        mx = (src[0] + dst[0]) / 2 + text_offset[0]
        my = (src[1] + dst[1]) / 2 + text_offset[1]
        ax.text(mx, my, text, ha="center", va="center", fontsize=7, color=COL_MUTED, style="italic")


def az_band(ax, xy, w, h, label):
    """Translucent shaded band representing an Availability Zone."""
    x, y = xy
    rect = patches.Rectangle(
        (x, y),
        w,
        h,
        linewidth=0.7,
        edgecolor="#9aa4b5",
        facecolor="#f6f7fa",
        alpha=0.55,
        linestyle="--",
    )
    ax.add_patch(rect)
    ax.text(
        x + 0.10,
        y + h - 0.15,
        label,
        ha="left",
        va="top",
        fontsize=8,
        color="#7b8493",
        weight="bold",
        style="italic",
    )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def render(out_path_png: Path, out_path_pdf: Path | None = None) -> None:
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H), dpi=200)
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 18)
    ax.set_aspect("equal")
    ax.axis("off")

    # Title
    ax.text(
        7,
        17.6,
        "Wahoo Bay — AWS Reference Architecture (Multi-AZ, fully redundant)",
        ha="center",
        va="top",
        fontsize=13,
        weight="bold",
        color=COL_TEXT,
    )
    ax.text(
        7,
        17.15,
        "Two cameras · active+standby workers per camera · Multi-AZ database · cross-region S3",
        ha="center",
        va="top",
        fontsize=9,
        style="italic",
        color=COL_MUTED,
    )

    # ── External cameras (top-left) ──────────────────────────────────────────
    box(
        ax,
        (0.3, 15.5),
        2.5,
        0.9,
        face=COL_EXTERN,
        edge=COL_EXTERN_BD,
        label="SEAHIVECAM",
        sublabel="Axis PTZ · RTSP H.264 · Pompano Bch",
    )
    box(
        ax,
        (0.3, 14.3),
        2.5,
        0.9,
        face=COL_EXTERN,
        edge=COL_EXTERN_BD,
        label="Pier cam",
        sublabel="Axis · RTSP H.264 · Pompano Bch",
    )

    # ── Public dashboard (top-right) ─────────────────────────────────────────
    box(
        ax,
        (11.0, 15.7),
        2.7,
        0.7,
        face=COL_NETWORK,
        edge=COL_NETWORK_BD,
        label="Public dashboard",
        sublabel="https://wahoobay.org",
    )
    box(
        ax,
        (11.0, 14.6),
        2.7,
        0.7,
        face=COL_NETWORK,
        edge=COL_NETWORK_BD,
        label="CloudFront",
        sublabel="TLS · edge cache · AWS Shield",
    )
    arrow(ax, (12.35, 15.7), (12.35, 15.3))

    # ── ALB (slightly below CloudFront) ──────────────────────────────────────
    box(
        ax,
        (10.6, 13.3),
        3.1,
        0.7,
        face=COL_NETWORK,
        edge=COL_NETWORK_BD,
        label="Application Load Balancer",
        sublabel="multi-AZ target group",
    )
    arrow(ax, (12.35, 14.6), (12.35, 14.0))

    # ── AWS region container (us-east-1) ─────────────────────────────────────
    region_rect = patches.Rectangle(
        (3.4, 0.6),
        10.4,
        13.6,
        linewidth=1.2,
        edgecolor="#a0a8b8",
        facecolor="none",
        linestyle="-",
    )
    ax.add_patch(region_rect)
    ax.text(
        3.6,
        13.95,
        "AWS region: us-east-1",
        ha="left",
        va="bottom",
        fontsize=8,
        color=COL_MUTED,
        weight="bold",
        style="italic",
    )

    # ── AZ bands inside region ───────────────────────────────────────────────
    az_band(ax, (3.6, 5.5), 5.0, 7.2, "us-east-1a")
    az_band(ax, (8.8, 5.5), 5.0, 7.2, "us-east-1b")

    # ── Workers — SEAHIVECAM (active in 1a, standby in 1b) ───────────────────
    # Active workers (us-east-1a)
    box(
        ax,
        (3.85, 11.5),
        2.4,
        1.0,
        face=COL_COMPUTE,
        edge=COL_COMPUTE_BD,
        label="Worker · g5.xlarge",
        sublabel="SEAHIVECAM · ACTIVE",
    )
    box(
        ax,
        (3.85, 10.2),
        2.4,
        1.0,
        face=COL_COMPUTE,
        edge=COL_COMPUTE_BD,
        label="Worker · g5.xlarge",
        sublabel="Pier cam · ACTIVE",
    )
    # Standby workers (us-east-1b)
    box(
        ax,
        (9.05, 11.5),
        2.4,
        1.0,
        face=COL_STANDBY,
        edge=COL_STANDBY_BD,
        label="Worker · g5.xlarge",
        sublabel="SEAHIVECAM · STANDBY",
    )
    box(
        ax,
        (9.05, 10.2),
        2.4,
        1.0,
        face=COL_STANDBY,
        edge=COL_STANDBY_BD,
        label="Worker · g5.xlarge",
        sublabel="Pier cam · STANDBY",
    )

    # RTSP into active workers + dashed RTSP into standby (passive)
    arrow(ax, (2.8, 15.95), (3.85, 12.5), text="RTSP/TCP", curvature=0.18, text_offset=(-0.5, -0.4))
    arrow(
        ax, (2.8, 14.75), (3.85, 10.85), text="RTSP/TCP", curvature=-0.15, text_offset=(-0.6, 0.1)
    )
    # Standby passive — dashed (we represent "ready but not consuming" by a faint arrow)
    arrow(
        ax,
        (2.8, 15.95),
        (9.05, 12.05),
        color="#c9a5a5",
        style="-|>",
        curvature=0.30,
        lw=0.8,
        text="passive (failover)",
        text_offset=(0.0, 0.4),
    )
    arrow(ax, (2.8, 14.75), (9.05, 10.65), color="#c9a5a5", style="-|>", curvature=0.30, lw=0.8)

    # Failover note across the two-worker pairs
    arrow(
        ax,
        (6.25, 12.0),
        (9.05, 12.0),
        color="#7c7c7c",
        style="<|-|>",
        lw=0.9,
        text="Route 53 health-check failover (~60s)",
        text_offset=(0, 0.15),
    )

    # ── Dashboard ASG (two AZs) ──────────────────────────────────────────────
    box(
        ax,
        (4.5, 8.4),
        2.0,
        0.9,
        face=COL_COMPUTE,
        edge=COL_COMPUTE_BD,
        label="Dashboard",
        sublabel="t4g.medium · 1a",
    )
    box(
        ax,
        (9.5, 8.4),
        2.0,
        0.9,
        face=COL_COMPUTE,
        edge=COL_COMPUTE_BD,
        label="Dashboard",
        sublabel="t4g.medium · 1b",
    )
    # ASG group bracket
    ax.plot([4.5, 11.5], [9.55, 9.55], color="#8a93a4", lw=0.6, ls=":")
    ax.text(
        8,
        9.7,
        "Auto Scaling Group (min=2)",
        ha="center",
        va="bottom",
        fontsize=7,
        color="#8a93a4",
        style="italic",
    )
    # ALB → dashboards
    arrow(ax, (11.0, 13.3), (5.5, 9.3), color="#8a93a4", curvature=0.25)
    arrow(ax, (12.0, 13.3), (10.5, 9.3), color="#8a93a4", curvature=-0.10)

    # ── RDS Multi-AZ ─────────────────────────────────────────────────────────
    box(
        ax,
        (4.5, 6.7),
        2.0,
        1.0,
        face=COL_DATA,
        edge=COL_DATA_BD,
        label="RDS PostgreSQL",
        sublabel="primary · 1a",
    )
    box(
        ax,
        (9.5, 6.7),
        2.0,
        1.0,
        face=COL_DATA,
        edge=COL_DATA_BD,
        label="RDS PostgreSQL",
        sublabel="sync standby · 1b",
    )
    arrow(
        ax,
        (6.5, 7.2),
        (9.5, 7.2),
        color=COL_DATA_BD,
        style="<|-|>",
        text="synchronous replication",
        text_offset=(0, 0.2),
    )
    # Workers + dashboards → RDS primary
    arrow(
        ax, (5.05, 10.2), (5.05, 7.7), color="#8a93a4", lw=0.8, text="SQL", text_offset=(-0.18, 0)
    )
    arrow(ax, (5.5, 8.4), (5.5, 7.7), color="#8a93a4", lw=0.8)
    arrow(ax, (10.5, 8.4), (6.5, 7.4), color="#8a93a4", lw=0.8, curvature=-0.18)

    # ── EFS multi-AZ ─────────────────────────────────────────────────────────
    box(
        ax,
        (6.7, 5.85),
        2.6,
        0.75,
        face=COL_DATA,
        edge=COL_DATA_BD,
        label="EFS multi-AZ",
        sublabel="shared saved-frames mount",
    )
    arrow(
        ax,
        (5.05, 10.2),
        (7.5, 6.6),
        color="#8a93a4",
        lw=0.8,
        text="JPEG saves",
        text_offset=(0.6, 0.3),
        curvature=-0.20,
    )
    arrow(ax, (10.25, 10.2), (8.5, 6.6), color="#8a93a4", lw=0.8, curvature=0.10)
    arrow(
        ax,
        (8.0, 6.6),
        (5.5, 8.4),
        color="#8a93a4",
        lw=0.8,
        text="dashboard reads",
        text_offset=(-0.4, 0.4),
        curvature=-0.20,
    )

    # ── S3 buckets (bottom row) ──────────────────────────────────────────────
    box(
        ax,
        (3.85, 1.6),
        4.2,
        1.4,
        face=COL_DATA,
        edge=COL_DATA_BD,
        label="S3 us-east-1",
        sublabel="frame archive (Glacier IT)\nmodel checkpoints\ndataset releases",
        font_size=9,
        sub_size=7,
    )
    box(
        ax,
        (9.4, 1.6),
        4.2,
        1.4,
        face=COL_DATA,
        edge=COL_DATA_BD,
        label="S3 us-west-2 (replica)",
        sublabel="cross-region disaster copy\nGlacier Deep Archive",
        font_size=9,
        sub_size=7,
    )
    arrow(
        ax,
        (8.05, 2.3),
        (9.4, 2.3),
        color=COL_DATA_BD,
        style="<|-|>",
        text="cross-region replication",
        text_offset=(0, 0.25),
    )
    # EFS → S3 daily archive
    arrow(
        ax,
        (7.5, 5.85),
        (6.0, 3.0),
        color="#8a93a4",
        lw=0.8,
        text="daily archive",
        text_offset=(-0.7, 0),
        curvature=0.20,
    )

    # ── Auxiliary services (left margin) ─────────────────────────────────────
    box(
        ax,
        (0.3, 8.8),
        2.8,
        0.65,
        face=COL_NETWORK,
        edge=COL_NETWORK_BD,
        label="Route 53 + ACM",
        sublabel="DNS, TLS cert",
        font_size=8,
        sub_size=6,
    )
    box(
        ax,
        (0.3, 7.8),
        2.8,
        0.65,
        face=COL_NETWORK,
        edge=COL_NETWORK_BD,
        label="ECR",
        sublabel="Docker image registry",
        font_size=8,
        sub_size=6,
    )
    box(
        ax,
        (0.3, 6.8),
        2.8,
        0.65,
        face=COL_NETWORK,
        edge=COL_NETWORK_BD,
        label="Secrets Manager",
        sublabel="RTSP creds, DB pw, tokens",
        font_size=8,
        sub_size=6,
    )
    box(
        ax,
        (0.3, 5.8),
        2.8,
        0.65,
        face=COL_NETWORK,
        edge=COL_NETWORK_BD,
        label="CloudWatch",
        sublabel="Logs · Metrics · Alarms",
        font_size=8,
        sub_size=6,
    )
    box(
        ax,
        (0.3, 4.8),
        2.8,
        0.65,
        face=COL_NETWORK,
        edge=COL_NETWORK_BD,
        label="AWS Backup",
        sublabel="RDS + EBS daily snapshots",
        font_size=8,
        sub_size=6,
    )
    box(
        ax,
        (0.3, 3.8),
        2.8,
        0.65,
        face=COL_COMPUTE,
        edge=COL_COMPUTE_BD,
        label="EC2 Spot (burst)",
        sublabel="quarterly retraining",
        font_size=8,
        sub_size=6,
    )

    # ── External / data-ingest box (left, low) ───────────────────────────────
    box(
        ax,
        (0.3, 2.0),
        2.8,
        1.55,
        face=COL_EXTERN,
        edge=COL_EXTERN_BD,
        label="External data sources",
        sublabel=(
            "api.wahoobay.net (sonde + station)\n"
            "api.sensestream.org\n"
            "open-meteo.com (forecast)\n"
            "weather.gov KPMP (METAR)\n"
            "iNaturalist (training only)\n"
            "WoRMS (taxonomy)"
        ),
        font_size=8,
        sub_size=6.5,
    )
    arrow(
        ax,
        (3.1, 2.8),
        (5.0, 6.7),
        color="#8a93a4",
        lw=0.8,
        text="poller writes",
        text_offset=(0.8, -0.2),
        curvature=-0.3,
    )

    # ── Legend ───────────────────────────────────────────────────────────────
    legend_x, legend_y = 0.3, 0.6

    def _swatch(x, y, face, edge, text):
        ax.add_patch(
            patches.Rectangle((x, y), 0.3, 0.20, facecolor=face, edgecolor=edge, linewidth=1.0)
        )
        ax.text(x + 0.4, y + 0.1, text, fontsize=7.5, color=COL_TEXT, va="center")

    _swatch(legend_x, legend_y, COL_COMPUTE, COL_COMPUTE_BD, "Active compute (EC2)")
    _swatch(legend_x + 2.6, legend_y, COL_STANDBY, COL_STANDBY_BD, "Standby compute (warm)")
    _swatch(legend_x + 5.2, legend_y, COL_DATA, COL_DATA_BD, "Data plane (RDS / EFS / S3)")
    _swatch(legend_x + 8.2, legend_y, COL_NETWORK, COL_NETWORK_BD, "Network / managed services")
    _swatch(legend_x + 11.2, legend_y, COL_EXTERN, COL_EXTERN_BD, "External (off-AWS)")

    plt.tight_layout()
    fig.savefig(out_path_png, dpi=200, bbox_inches="tight", facecolor="white")
    if out_path_pdf is not None:
        fig.savefig(out_path_pdf, format="pdf", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"wrote {out_path_png}")
    if out_path_pdf is not None:
        print(f"wrote {out_path_pdf}")


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = repo_root / "docs"
    render(out_dir / "grant_arch.png", out_dir / "grant_arch.pdf")
