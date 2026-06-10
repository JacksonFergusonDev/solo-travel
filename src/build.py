from pathlib import Path
from string import Template

import pandas as pd

from engine import ItineraryDAG

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def generate_dashboard():
    df_stays = pd.read_csv(PROJECT_ROOT / "data" / "stays.csv")
    df_transit = pd.read_csv(PROJECT_ROOT / "data" / "transit.csv")
    df_accomm = pd.read_csv(PROJECT_ROOT / "data" / "accommodations.csv")
    # ADD THIS: Ingest shows dataframe
    df_shows = pd.read_csv(PROJECT_ROOT / "data" / "shows.csv")

    trip = ItineraryDAG()
    trip.load_from_dataframes(df_stays, df_transit, df_accomm, df_shows)

    print("Executing temporal consistency checks...")
    trip.validate_timeline()

    metrics = trip.get_financial_summary()
    df_view = trip.get_itinerary_df()

    table_html = df_view.to_html(
        classes="table table-dark table-hover align-middle custom-table",
        border=0,
        justify="left",
        index=False,
        escape=False,
    )

    m = trip.plot_geographic_map()
    map_html = m.get_root().render()

    cal_data = trip.get_calendar_data()

    template_path = PROJECT_ROOT / "src" / "templates" / "dashboard.html"
    with open(template_path, encoding="utf-8") as file:
        template = Template(file.read())

    final_html = template.safe_substitute(
        total_cad=f"{metrics['total_cad']:,}",
        stay_cad=f"{metrics['stay_cad']:,}",
        transit_cad=f"{metrics['transit_cad']:,}",
        event_cad=f"{metrics['event_cad']:,}",
        table_html=table_html,
        map_html=map_html,
        calendar_events=cal_data["events_json"],
    )

    output_path = PROJECT_ROOT / "index.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(final_html)

    print(f"Compilation complete. Output tracking written to {output_path}.")


if __name__ == "__main__":
    generate_dashboard()
