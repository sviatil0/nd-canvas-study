"""Render topic dependency graph as PNG using networkx + matplotlib.

Layered (Sugiyama-style) layout: each topic placed at its longest-prereq depth
on the y-axis and chapter on the x-axis. Curved edges, big fonts, no overlap.

    python render_graph.py --course-dir downloads/128781_statistics
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx

from topic_graph import GRAPH, for_course as graph_for_course


def build() -> nx.DiGraph:
    g = nx.DiGraph()
    for k, v in GRAPH.items():
        g.add_node(k, label=v["label"], ch=v["ch"])
    for k, v in GRAPH.items():
        for p in v["prereqs"]:
            if p in GRAPH:
                g.add_edge(p, k)
    return g


def longest_path_layers(g: nx.DiGraph) -> dict[str, int]:
    """Assign each node a layer = length of longest path from any root."""
    depth = {n: 0 for n in g.nodes}
    for n in nx.topological_sort(g):
        for s in g.successors(n):
            if depth[s] < depth[n] + 1:
                depth[s] = depth[n] + 1
    return depth


def layered_positions(g: nx.DiGraph) -> dict[str, tuple[float, float]]:
    layers = longest_path_layers(g)
    by_layer: dict[int, list[str]] = defaultdict(list)
    for n, l in layers.items():
        by_layer[l].append(n)
    # Within a layer, sort by chapter so flow tracks course timeline
    pos = {}
    max_layer = max(by_layer)
    for l, nodes in by_layer.items():
        nodes.sort(key=lambda n: (g.nodes[n]["ch"], n))
        # Spread x evenly within the layer
        n_count = len(nodes)
        for i, n in enumerate(nodes):
            x = (i + 0.5) * (10.0 / max(n_count, 1))
            y = max_layer - l  # roots on top
            pos[n] = (x, y)
    return pos


def render(out_path: Path) -> None:
    g = build()
    pos = layered_positions(g)

    fig, ax = plt.subplots(figsize=(22, 14))

    # Color nodes by chapter group
    cmap = plt.colormaps.get_cmap("tab20")
    chapters = sorted({d["ch"] for _, d in g.nodes(data=True)})
    ch_color = {c: cmap(i / max(len(chapters) - 1, 1)) for i, c in enumerate(chapters)}
    node_colors = [ch_color[g.nodes[n]["ch"]] for n in g.nodes]

    nx.draw_networkx_edges(
        g, pos, ax=ax,
        arrows=True, arrowsize=22, arrowstyle="-|>",
        edge_color="#666", width=1.6, alpha=0.7,
        connectionstyle="arc3,rad=0.12",
        min_source_margin=20, min_target_margin=22,
    )
    nx.draw_networkx_nodes(
        g, pos, ax=ax,
        node_color=node_colors, node_shape="s",
        node_size=5800, edgecolors="#222", linewidths=1.4,
        alpha=0.95,
    )
    labels = {n: f"Ch{d['ch']}\n{d['label']}" for n, d in g.nodes(data=True)}
    nx.draw_networkx_labels(g, pos, labels=labels, ax=ax, font_size=9, font_weight="bold")

    ax.set_title("ACMS 30440 — Topic Dependency Graph", fontsize=16, weight="bold", pad=14)
    ax.set_xlim(-1, 11)
    ax.margins(0.05)
    ax.axis("off")
    plt.tight_layout()
    fig.savefig(out_path, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"wrote {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    args = ap.parse_args()
    cdir = Path(args.course_dir)

    global GRAPH
    from analyze import detect_course_id
    GRAPH = graph_for_course(detect_course_id(cdir) or 0) or GRAPH
    out_dir = cdir / "bundles"
    out_dir.mkdir(parents=True, exist_ok=True)
    render(out_dir / "topic_graph.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
