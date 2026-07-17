import csv
import os
from pathlib import Path
import time
import pandas as pd
import numpy as np
import folium
from typing import Any, Dict, List
from pyomo.environ import (
    ConcreteModel, Set, Param, Var, Binary, NonNegativeReals,
    Objective, Constraint, minimize, value
)
from pyomo.opt import SolverFactory, TerminationCondition
from ICARUS.util.constants import FIBER_DELAY_US_PER_KM, MAX_FIBER_DISTANCE_KM, MAX_LOAD
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()
MAX_CLUSTER_SIZE = int(os.environ["MAX_RUS"])

def generate_map(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
    output_filename: Path
):
    """
    Gera um mapa HTML da clusterização ILP RU -> O-DU.
    """
    tiles: str = "OpenStreetMap"
    df = df.copy()
    df["id"] = df["id"].astype(int)
    df["O-DU"] = df["O-DU"].astype(int) 
    df_by_id = df.set_index("id", drop=False)
    du_ids = set(df.loc[df["id"] == df["O-DU"], "id"])

    # Centro inicial do mapa
    center_lat = df["Lat"].mean()
    center_lon = df["Lon"].mean()

    mapa = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        tiles=tiles,
        control_scale=True,
    )

    fg_du = folium.FeatureGroup(name="DUs", show=True)
    fg_ru = folium.FeatureGroup(name="RUs", show=True)
    fg_links = folium.FeatureGroup(name="Conexões RU-DU", show=True)
    fg_labels = folium.FeatureGroup(name="Labels", show=True)

    # ------------------------------------------------------------------
    # 1. Linhas RU -> DU com label de distância
    # ------------------------------------------------------------------
    for _, row in df.iterrows():
        ru_id = row["id"]
        du_id = row["O-DU"]

        # Não desenha linha da DU para ela mesma
        if ru_id == du_id:
            continue

        ru_lat = row["Lat"]
        ru_lon = row["Lon"]

        du_row = df_by_id.loc[du_id]
        du_lat = du_row["Lat"]
        du_lon = du_row["Lon"]

        distancia_km = float(df_dm.loc[ru_id, du_id])

        # Linha RU -> DU
        linha = folium.PolyLine(
            locations=[
                [ru_lat, ru_lon],
                [du_lat, du_lon],
            ],
            color="blue",
            weight=1,
            opacity=0.65,
            tooltip=(
                f"RU {ru_id} → DU {du_id}<br>"
                f"Distância: {distancia_km:.3f} km"
            ),
        )
        linha.add_to(fg_links)

        # Label fixo da distância no ponto médio da linha
        mid_lat = (ru_lat + du_lat) / 2
        mid_lon = (ru_lon + du_lon) / 2

        folium.Marker(
            location=[mid_lat, mid_lon],
            icon=folium.DivIcon(
                html=f"""
                <div style="
                    font-size: 9px;
                    color: #003366;
                    background-color: rgba(255, 255, 255, 0.85);
                    border: 1px solid #003366;
                    border-radius: 4px;
                    padding: 2px 4px;
                    white-space: nowrap;">
                    {distancia_km:.2f} km
                </div>
                """
            ),
        ).add_to(fg_labels)

    # ------------------------------------------------------------------
    # 2. Marcadores de DUs e RUs
    # ------------------------------------------------------------------
    for _, row in df.iterrows():
        station_id = row["id"]
        lat = row["Lat"]
        lon = row["Lon"]
        bandwidth = row["bandwidth"]
        assigned_du = row["O-DU"]

        is_du = station_id == assigned_du

        popup_html = f"""
        <b>Estação:</b> {station_id}<br>
        <b>Tipo:</b> {"DU" if is_du else "RU"}<br>
        <b>Bandwidth:</b> {bandwidth} MHz<br>
        <b>O-DU associada:</b> {assigned_du}
        """

        if is_du:
            marker = folium.Marker(
                location=[lat, lon],
                tooltip=f"DU {station_id} | BW: {bandwidth} MHz",
                popup=folium.Popup(popup_html, max_width=300),
                icon=folium.Icon(
                    color="red",
                    icon="server",
                    prefix="fa",
                ),
            )
            marker.add_to(fg_du)

            label_text = f"DU {station_id}<br>{bandwidth} MHz"
            label_color = "#8B0000"
            border_color = "#8B0000"

        else:
            marker = folium.Marker(
                location=[lat, lon],
                tooltip=f"RU {station_id} | BW: {bandwidth} MHz | DU: {assigned_du}",
                popup=folium.Popup(popup_html, max_width=300),
                icon=folium.Icon(
                    color="green",
                    icon="wifi",
                    prefix="fa",
                ),
            )
            marker.add_to(fg_ru)

            label_text = f"RU {station_id}<br>{bandwidth} MHz"
            label_color = "#006400"
            border_color = "#006400"

        # Label fixo de bandwidth ao lado do marcador
        folium.Marker(
            location=[lat, lon],
            icon=folium.DivIcon(
                icon_anchor=(-8, 18),
                html=f"""
                <div style="
                    font-size: 9px;
                    color: {label_color};
                    background-color: rgba(255, 255, 255, 0.85);
                    border: 1px solid {border_color};
                    border-radius: 4px;
                    padding: 2px 4px;
                    white-space: nowrap;">
                    {label_text}
                </div>
                """
            ),
        ).add_to(fg_labels)

    fg_links.add_to(mapa)
    fg_du.add_to(mapa)
    fg_ru.add_to(mapa)
    fg_labels.add_to(mapa)

    folium.LayerControl(collapsed=False).add_to(mapa)

    # Ajusta o zoom para cobrir todas as estações
    bounds = df[["Lat", "Lon"]].values.tolist()
    mapa.fit_bounds(bounds)
    print(f"Salvando mapa interativo em {output_filename}...")
    mapa.save(output_filename)

    return True


if __name__ == "__main__":
    csv_path = OUT_DIR / f"dm_{Filename}.csv"
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Matriz de distâncias não encontrada: {csv_path}")
    # Carrega a Matriz de Distâncias. Define 'id' como índice para acesso rápido: dists.loc[id_origem, id_destino]
    df_dm = pd.read_csv(csv_path, index_col='id')
    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    #abrindo o arquivo resultado da clusterização RU-DU com critério Max Load
    csv_path = OUT_DIR / f"ilp_{Filename}_max_load.csv"
    print(f"Carregando o arquivo {csv_path}")
    df_cluster = pd.read_csv(csv_path)
    
    #Gerando Mapa Max Load
    generate_map(df_cluster, df_dm, output_filename=OUT_DIR / f"map_{Filename}_max_load.html")

    #abrindo o arquivo resultado da clusterização RU-DU com critério Total Distance
    csv_path = OUT_DIR / f"ilp_{Filename}_total_distance.csv"
    print(f"Carregando o arquivo {csv_path}")
    df_cluster = pd.read_csv(csv_path)

    #Gerando Mapa Total Distance
    generate_map(df_cluster, df_dm, output_filename=OUT_DIR / f"map_{Filename}_total_distance.html")

    


