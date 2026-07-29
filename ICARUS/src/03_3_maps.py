import os
from pathlib import Path

import contextily as ctx
import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from shapely import concave_hull
from shapely.geometry import LineString, MultiPoint
from shapely.ops import unary_union
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()
BASEMAP_FILE = OUT_DIR / f"basemap_{Filename}_osm.tif"
FIGSIZE = (7.2, 7.2)
CONCAVE_HULL_RATIO = 0.35
CLUSTER_BUFFER_M = 260
AREA_BUFFER_M = 650
MAP_PADDING = 0.055

def create_cluster_boundary(points):
    """Cria o contorno do cluster, inclusive para clusters com poucos pontos."""
    points = list(points)

    if len(points) == 1:
        return points[0].buffer(CLUSTER_BUFFER_M)

    if len(points) == 2:
        return LineString(points).buffer(
            CLUSTER_BUFFER_M,
            cap_style="round",
            join_style="round",
        )

    multipoint = MultiPoint(points)
    boundary = concave_hull(
        multipoint,
        ratio=CONCAVE_HULL_RATIO,
        allow_holes=False,
    )

    if boundary.geom_type not in {"Polygon", "MultiPolygon"}:
        boundary = multipoint.convex_hull

    return boundary.buffer(CLUSTER_BUFFER_M, join_style="round")


def add_scale_bar(ax, bounds):
    """Adiciona uma barra de escala simples em metros ou quilômetros."""
    minx, miny, maxx, maxy = bounds
    width = maxx - minx
    height = maxy - miny

    target = width * 0.18
    options = np.array([100, 200, 500, 1000, 2000, 5000, 10000, 20000])
    valid = options[options <= target]
    length = int(valid[-1] if len(valid) else options[0])

    x = minx + width * 0.06
    y = miny + height * 0.055
    tick = height * 0.007

    ax.plot([x, x + length], [y, y], color="black", linewidth=1.4, zorder=20)
    ax.plot([x, x], [y - tick, y + tick], color="black", linewidth=1.0, zorder=20)
    ax.plot(
        [x + length, x + length],
        [y - tick, y + tick],
        color="black",
        linewidth=1.0,
        zorder=20,
    )

    label = f"{length / 1000:g} km" if length >= 1000 else f"{length} m"
    ax.text(
        x + length / 2,
        y + height * 0.013,
        label,
        ha="center",
        va="bottom",
        fontsize=7,
    )


def add_north_arrow(ax):
    """Adiciona indicação discreta de norte."""
    ax.annotate(
        "N",
        xy=(0.94, 0.94),
        xytext=(0.94, 0.86),
        xycoords="axes fraction",
        textcoords="axes fraction",
        ha="center",
        va="center",
        fontsize=9,
        fontweight="bold",
        arrowprops={
            "arrowstyle": "-|>",
            "facecolor": "black",
            "edgecolor": "black",
            "linewidth": 1.0,
        },
        zorder=30,
    )

def add_openstreetmap_basemap(ax, base_geo, metric_crs, basemap_file):
    """
    Adiciona uma base cartográfica do OpenStreetMap.

    O arquivo GeoTIFF é baixado somente quando ainda não existe.
    Nas execuções seguintes, o arquivo local é reutilizado.
    """
    basemap_file = Path(basemap_file)

    if not basemap_file.exists():
        print(f"Baixando mapa-base do OpenStreetMap para {basemap_file}")

        # O contextily trabalha nativamente com tiles em Web Mercator.
        base_web = base_geo.to_crs("EPSG:3857")

        web_area = unary_union(
            base_web.geometry.tolist()
        ).convex_hull.buffer(AREA_BUFFER_M)

        minx, miny, maxx, maxy = web_area.bounds

        ctx.bounds2raster(
            minx,
            miny,
            maxx,
            maxy,
            path=basemap_file,
            zoom=11,
            source=ctx.providers.OpenStreetMap.Mapnik,
        )

    print(f"Carregando mapa-base local {basemap_file}")

    ctx.add_basemap(
        ax,
        crs=metric_crs,
        source=basemap_file,
        alpha=0.48,
        reset_extent=True,
        zorder=0,
    )

def generate_map(base, clusters, output):
    """
    Gera um mapa PDF de um cenário.

    Parameters
    ----------
    base : pandas.DataFrame
        Base comum com NumEstacao, Lat e Lon.
    clusters : pandas.DataFrame
        Resultado ILP com NumEstacao, Lat, Lon e O-DU.
    output : pathlib.Path
        Caminho do PDF de saída.
    """
    output = Path(output)

    base = base.copy()
    clusters = clusters.copy()

    base["NumEstacao"] = base["NumEstacao"].astype("int64")
    clusters["NumEstacao"] = clusters["NumEstacao"].astype("int64")
    clusters["O-DU"] = clusters["O-DU"].astype("int64")
    clusters["O-DU_ID"] = clusters["O-DU_ID"].astype("int64")

    base_geo = gpd.GeoDataFrame(
        base,
        geometry=gpd.points_from_xy(base["Lon"], base["Lat"]),
        crs="EPSG:4326",
    )

    metric_crs = base_geo.estimate_utm_crs()
    base_geo = base_geo.to_crs(metric_crs)

    clusters_geo = gpd.GeoDataFrame(
        clusters,
        geometry=gpd.points_from_xy(clusters["Lon"], clusters["Lat"]),
        crs="EPSG:4326",
    ).to_crs(metric_crs)

    # O enquadramento é calculado exclusivamente pela base comum.
    area = unary_union(base_geo.geometry.tolist()).convex_hull.buffer(
        AREA_BUFFER_M,
        join_style="round",
    )

    minx, miny, maxx, maxy = area.bounds
    padding = max(maxx - minx, maxy - miny) * MAP_PADDING
    bounds = (
        minx - padding,
        miny - padding,
        maxx + padding,
        maxy + padding,
    )

    cluster_ids = sorted(clusters_geo["O-DU"].unique())
    colors = plt.get_cmap("tab20", len(cluster_ids))
    cluster_colors = {
        cluster_id: colors(index)
        for index, cluster_id in enumerate(cluster_ids)
    }

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8,
            "pdf.fonttype": 42,
        }
    )

    fig, ax = plt.subplots(figsize=FIGSIZE)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#fafafa")

    # Base cartográfica discreta: ruas e massas d'água, sem rótulos.
    # Os mosaicos são rasterizados dentro do PDF; os clusters permanecem vetoriais.
    ax.set_xlim(bounds[0], bounds[2])
    ax.set_ylim(bounds[1], bounds[3])
    add_openstreetmap_basemap(
        ax=ax,
        base_geo=base_geo,
        metric_crs=metric_crs,
        basemap_file=BASEMAP_FILE,
    )

    # Limite simplificado da área analisada.
    gpd.GeoSeries([area], crs=metric_crs).plot(
        ax=ax,
        facecolor="none",
        edgecolor="#888888",
        linewidth=0.7,
        linestyle=(0, (3, 2)),
        zorder=1,
    )

    for cluster_id, group in clusters_geo.groupby("O-DU", sort=True):
        color = cluster_colors[cluster_id]
        boundary = create_cluster_boundary(group.geometry)

        # Preenchimento e contorno do cluster.
        gpd.GeoSeries([boundary], crs=metric_crs).plot(
            ax=ax,
            facecolor=color,
            edgecolor="none",
            alpha=0.10,
            zorder=2,
        )
        gpd.GeoSeries([boundary], crs=metric_crs).boundary.plot(
            ax=ax,
            color=color,
            linewidth=1.0,
            alpha=0.90,
            zorder=3,
        )

        du = group.loc[group["NumEstacao"] == cluster_id].iloc[0]
        du_point = du.geometry

        # Enlaces O-DU–O-RU.
        for _, ru in group.iterrows():
            if ru["NumEstacao"] == cluster_id:
                continue

            ax.plot(
                [du_point.x, ru.geometry.x],
                [du_point.y, ru.geometry.y],
                color=color,
                linewidth=0.55,
                alpha=0.40,
                zorder=4,
            )

        # O-RUs.
        rus = group.loc[group["NumEstacao"] != cluster_id]
        if not rus.empty:
            rus.plot(
                ax=ax,
                marker="o",
                markersize=18,
                color=color,
                edgecolor="white",
                linewidth=0.4,
                zorder=6,
            )

        # O-DU identificador.
        ax.scatter(
            du_point.x,
            du_point.y,
            marker="s",
            s=54,
            facecolor=color,
            edgecolor="black",
            linewidth=0.8,
            zorder=10,
        )
        ax.annotate(
            str(int(du["O-DU_ID"])),
            xy=(du_point.x, du_point.y),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=5.6,
            fontweight="semibold",
            ha="left",
            va="bottom",
            bbox={
                "boxstyle": "round,pad=0.10",
                "facecolor": "white",
                "edgecolor": "none",
                "alpha": 0.82,
            },
            zorder=12,
        )

    ax.set_xlim(bounds[0], bounds[2])
    ax.set_ylim(bounds[1], bounds[3])
    ax.set_aspect("equal", adjustable="box")

    # Remove eixos e coordenadas para manter a figura limpa.
    ax.tick_params(
        left=False,
        bottom=False,
        labelleft=False,
        labelbottom=False,
    )
    for spine in ax.spines.values():
        spine.set_visible(False)

    legend = [
        Line2D(
            [0], [0],
            marker="o",
            linestyle="none",
            markerfacecolor="#777777",
            markeredgecolor="white",
            markersize=5.5,
            label="O-RU",
        ),
        Line2D(
            [0], [0],
            marker="s",
            linestyle="none",
            markerfacecolor="#777777",
            markeredgecolor="black",
            markersize=6.5,
            label="O-DU",
        ),
        Line2D(
            [0], [0],
            color="#777777",
            linewidth=0.8,
            label="Network Link O-DU<–>O-RU",
        ),
        Patch(
            facecolor="#bbbbbb",
            edgecolor="#777777",
            alpha=0.25,
            label="Cluster",
        ),
        Line2D(
            [0], [0],
            color="#aaaaaa",
            linewidth=0.8,
            linestyle=(0, (3, 2)),
            label="Area analyzed",
        ),
    ]

    ax.legend(
        handles=legend,
        loc="lower right",
        frameon=True,
        framealpha=0.92,
        facecolor="white",
        edgecolor="#bbbbbb",
        fontsize=7,
        borderpad=0.5,
        labelspacing=0.4,
        handlelength=2.0,
    )

    add_scale_bar(ax, bounds)
    add_north_arrow(ax)

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        output,
        format="pdf",
        bbox_inches="tight",
        pad_inches=0.06,
        facecolor="white",
    )
    plt.close(fig)

    print(f"Mapa gerado: {output}")

    return True


if __name__ == "__main__":
    #abrindo o arquivo com base de dados de RUs e DUs com informações de latitude, longitude e bandwidth
    csv_path = OUT_DIR / f"grp_{Filename}.csv"
    print(f"Carregando o arquivo {csv_path}")
    base = pd.read_csv(csv_path)
    ta = pd.read_csv(OUT_DIR / f"ta_{Filename}.csv")
    #abrindo o arquivos de clusterização
    for prefixo in ("ilp_", "grd_"):
        padrao = f"{prefixo}{Filename}_*.csv"
        for arquivo_csv in OUT_DIR.glob(padrao):
            #abrindo o arquivo de  clusterização
            csv_path = arquivo_csv
            print(f"Carregando o arquivo {csv_path}")
            clusters = pd.read_csv(csv_path)
            cadeia = arquivo_csv.stem.split(f"{prefixo}{Filename}_", 1)[1]
            clusters = clusters.merge(ta, on="O-DU", how="left", validate="m:1")
            #Gerando os mapas de clusterização
            generate_map(base, clusters, output=OUT_DIR / f"map_{prefixo}{Filename}_{cadeia}.pdf")


