import json
import os
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from string import Template

import folium
import networkx as nx
import pandas as pd
from dateutil import parser as date_parser
from folium.plugins import BeautifyIcon
from geopy.geocoders import Nominatim

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ItineraryDAG:
    def __init__(
        self, cache_file=PROJECT_ROOT / ".pipeline_cache" / "pipeline_cache.json"
    ):
        self.pipeline = nx.DiGraph()
        self.geolocator = Nominatim(user_agent="jackson_euro_pipeline")

        # Ensure the .pipeline_cache directory exists before trying to read/write to it
        self.cache_file = Path(cache_file)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

        self.geo_cache = self._load_cache()

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            with open(self.cache_file) as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return {}
        return {}

    def _save_cache(self):
        with open(self.cache_file, "w") as f:
            json.dump(self.geo_cache, f)

    def _parse_with_year(self, date_str):
        if pd.isna(date_str) or str(date_str).strip() == "":
            return None

        date_str = str(date_str)
        if "2026" not in date_str:
            date_str = f"{date_str} 2026"
        return date_parser.parse(date_str)

    def _get_cad_conversion(self, amount: float, currency_code: str) -> float:
        """Converts local currencies to CAD using a 24-hour cache window
        to minimize API overhead."""
        if pd.isna(amount) or amount == 0.0:
            return 0.0

        currency_code = str(currency_code).upper().strip()
        if currency_code == "CAD":
            return round(float(amount), 2)

        current_time = time.time()
        rates_cache = self.geo_cache.get("__rates_cache__", {})

        # Cache Validation (86400 seconds = 24 hours TTL)
        if rates_cache and (current_time - rates_cache.get("timestamp", 0) < 86400):
            conversion_rates = rates_cache["cad_conversions"]
        else:
            print("Cache miss or TTL expired for exchange rates. Querying API...")
            try:
                url = "https://open.er-api.com/v6/latest/CAD"
                with urllib.request.urlopen(url, timeout=5) as response:
                    data = json.loads(response.read().decode())
                    conversion_rates = data.get("rates", {})

                self.geo_cache["__rates_cache__"] = {
                    "timestamp": current_time,
                    "cad_conversions": conversion_rates,
                }
                self._save_cache()
            except Exception as e:
                print(
                    f"Warning: Exchange rate sync failed ({e})."
                    "Attempting fallback to expired cache..."
                )
                if rates_cache:
                    conversion_rates = rates_cache["cad_conversions"]
                else:
                    return 0.0

        rate = conversion_rates.get(currency_code)
        if rate:
            return round(amount / rate, 2)

        print(f"Warning: Currency token '{currency_code}' not found in registry.")
        return 0.0

    def add_waypoint(
        self,
        node_id: str,
        location: str,
        start_date: str,
        end_date: str,
        purpose: str,
        attributes: dict | None = None,
    ):
        attrs = attributes or {}
        start = self._parse_with_year(start_date)
        end = self._parse_with_year(end_date)

        # Graceful fallback: If end_date is missing, assume it's a 0-day stay.
        # This keeps the DAG alive and lets validate_timeline() naturally flag the gap.
        if end is None and start is not None:
            end = start

        # Calculate duration safely
        duration = (end - start).days if start and end else 0

        self.pipeline.add_node(
            node_id,
            location=location,
            start=start,
            end=end,
            duration=duration,
            purpose=purpose if not pd.isna(purpose) else "TBD",
            **attrs,
        )

    def load_from_dataframes(
        self,
        df_stays: pd.DataFrame,
        df_transit: pd.DataFrame,
        df_accomm: pd.DataFrame,
        df_shows: pd.DataFrame,  # Added df_shows
    ):
        """Ingests data layers, converts pricing to baseline CAD,
        and compiles the NetworkX topology."""

        # 0. Pre-process Shows (Parse datetimes and calculate CAD)
        df_shows["start_dt"] = pd.to_datetime(df_shows["start_datetime"])
        df_shows["end_dt"] = pd.to_datetime(df_shows["end_datetime"])
        df_shows["cost_cad"] = df_shows.apply(
            lambda row: self._get_cad_conversion(
                row.get("cost_local", 0.0), row.get("currency", "CAD")
            ),
            axis=1,
        )
        self.shows_data = df_shows.to_dict("records")  # Store globally for calendar

        # Aggregate show strings for the map nodes
        df_shows_agg = (
            df_shows.groupby("stay_id")
            .agg({"event_name": lambda x: "<br>• ".join(x.dropna().astype(str))})
            .rename(columns={"event_name": "shows_list"})
            .reset_index()
        )

        # 1. Pre-process accommodations
        df_accomm_agg = (
            df_accomm.groupby("stay_id")
            .agg(
                {
                    "hostel_name": lambda x: "<br>".join(x.dropna().astype(str)),
                    "cost_local": "sum",
                    "currency": "first",
                    "address": "first",
                    "contact_info": "first",
                    "check_in_time": "first",
                    "check_out_time": "first",
                    "booking_ref": lambda x: " | ".join(x.dropna().astype(str)),
                }
            )
            .reset_index()
        )

        # Merge everything into stays
        df_combined_stays = pd.merge(df_stays, df_accomm_agg, on="stay_id", how="left")
        df_combined_stays = pd.merge(
            df_combined_stays, df_shows_agg, on="stay_id", how="left"
        )

        # Helper to scrub NaNs out of string fields
        def _safe_str(val, default="TBD"):
            return default if pd.isna(val) or str(val).strip() == "" else str(val)

        # 1. Parse Stationary State Fields (Nodes)
        for _, row in df_combined_stays.iterrows():
            local_cost = row.get("cost_local", 0.0)
            currency = _safe_str(row.get("currency"), "CAD")
            cad_cost = self._get_cad_conversion(local_cost, currency)

            self.add_waypoint(
                node_id=row["stay_id"],
                location=row["location"],
                start_date=row["start_date"],
                end_date=row["end_date"],
                purpose=_safe_str(row.get("purpose")),
                attributes={
                    "hostel": _safe_str(row.get("hostel_name")),
                    "address": _safe_str(row.get("address")),
                    "contact": _safe_str(row.get("contact_info")),
                    "check_in": _safe_str(row.get("check_in_time")),
                    "check_out": _safe_str(row.get("check_out_time")),
                    "cost_local": local_cost,
                    "currency": currency,
                    "cost_cad": cad_cost,
                    "booking_ref": _safe_str(row.get("booking_ref")),
                    "shows_list": _safe_str(row.get("shows_list"), ""),
                },
            )

        # 2. Parse Relational Vectors (Edges)
        # Drop rows where origin_id or dest_id is NaN to prevent phantom parsing
        for _, row in df_transit.dropna(subset=["origin_id", "dest_id"]).iterrows():
            origin = row["origin_id"]
            dest = row["dest_id"]

            # HARDENING: Prevent NetworkX from generating phantom nodes
            if not self.pipeline.has_node(origin):
                print(
                    f"PIPELINE FAULT: Transit origin '{origin}'"
                    "not found in stays. Dropping edge."
                )
                continue
            if not self.pipeline.has_node(dest):
                print(
                    f"PIPELINE FAULT: Transit destination '{dest}'"
                    "not found in stays. Dropping edge."
                )
                continue

            local_cost = row.get("cost_local", 0.0)
            currency = _safe_str(row.get("currency"), "CAD")
            cad_cost = self._get_cad_conversion(local_cost, currency)

            self.pipeline.add_edge(
                origin,
                dest,
                mode=_safe_str(row.get("transit_mode")),
                carrier=_safe_str(row.get("carrier")),
                departure=_safe_str(row.get("departure_time")),
                arrival=_safe_str(row.get("arrival_time")),
                duration_hrs=row.get("duration_hrs", 0),
                cost_local=local_cost,
                currency=currency,
                cost_cad=cad_cost,
                ref=_safe_str(row.get("booking_ref")),
                transfers=_safe_str(row.get("transfer_details"), "None"),
            )

    def validate_timeline(self):
        if not nx.is_directed_acyclic_graph(self.pipeline):
            print("CRITICAL ERROR: Closed temporal loop detected in pipeline layout.")
            return False

        sorted_nodes = list(nx.topological_sort(self.pipeline))
        issues = []

        for i in range(len(sorted_nodes) - 1):
            u = sorted_nodes[i]
            v = sorted_nodes[i + 1]
            current_node = self.pipeline.nodes[u]
            next_node = self.pipeline.nodes[v]
            slack = (next_node["start"] - current_node["end"]).days

            if slack < 0:
                issues.append(
                    f"COLLISION: '{current_node['location']}' overlaps with "
                    f"'{next_node['location']}' by {abs(slack)} days."
                )
            elif slack > 0:
                # Topologically, edges representing overnight transit
                # bridge the calendar boundary.

                # Suppress the warning if slack is
                # exactly 1 day and an active edge connects the nodes.

                if slack == 1 and self.pipeline.has_edge(u, v):
                    continue

                issues.append(
                    f"GAP DETECTED: {slack} unallocated days between "
                    f"'{current_node['location']}' and '{next_node['location']}'."
                )

        if not issues:
            print("Pipeline verification successful. Zero date runtime collisions.")
        else:
            for issue in issues:
                print(issue)
        return len(issues) == 0

    def get_financial_summary(self):
        total_stay = sum(
            d.get("cost_cad", 0.0) for _, d in self.pipeline.nodes(data=True)
        )
        total_transit = sum(
            d.get("cost_cad", 0.0) for _, _, d in self.pipeline.edges(data=True)
        )
        total_events = sum(show.get("cost_cad", 0.0) for show in self.shows_data)

        return {
            "stay_cad": round(total_stay, 2),
            "transit_cad": round(total_transit, 2),
            "event_cad": round(total_events, 2),
            "total_cad": round(total_stay + total_transit + total_events, 2),
        }

    def get_itinerary_df(self):
        sorted_nodes = list(nx.topological_sort(self.pipeline))
        data = []

        for step, node_id in enumerate(sorted_nodes, start=1):
            node = self.pipeline.nodes[node_id]
            in_edges = list(self.pipeline.in_edges(node_id, data=True))

            if in_edges:
                edge_data = in_edges[0][2]
                transit_carrier = edge_data.get("carrier", "TBD")
                transit_mode = edge_data.get("mode", "TBD")
                transit_str = f"{transit_carrier} ({transit_mode})"
                transit_cost = f"${edge_data.get('cost_cad', 0.0):.2f}"
            else:
                transit_str = "Origin"
                transit_cost = "$0.00"

            data.append(
                {
                    "Step": step,
                    "Location": node["location"],
                    "Arrival": node["start"].strftime("%B %d"),
                    "Departure": node["end"].strftime("%B %d"),
                    "Days": node["duration"],
                    "Inbound Transit": transit_str,
                    "Transit (CAD)": transit_cost,
                    "Lodging": node.get("hostel", "TBD"),
                    "Lodging (CAD)": f"${node.get('cost_cad', 0.0):.2f}",
                }
            )

        return pd.DataFrame(data)

    def get_calendar_data(self):
        """Serializes the routing topology into a temporal format for FullCalendar."""
        sorted_nodes = list(nx.topological_sort(self.pipeline))
        events = []
        min_date = None

        # 1. GENERATE STAY EVENTS (Nodes)
        for node_id in sorted_nodes:
            node = self.pipeline.nodes[node_id]
            start_date = node["start"]
            end_date = node["end"]

            if start_date:
                if not min_date or start_date < min_date:
                    min_date = start_date

                start_str = start_date.strftime("%Y-%m-%d")
                end_str = end_date.strftime("%Y-%m-%d") if end_date else start_str

                # Pull inbound edge for metadata
                in_edges = list(self.pipeline.in_edges(node_id, data=True))
                if in_edges:
                    edge_data = in_edges[0][2]
                    carrier = edge_data.get("carrier", "TBD")
                    mode = edge_data.get("mode", "TBD")
                    transit_in = f"{carrier} ({mode})"
                    arr_time = edge_data.get("arrival", "TBD")
                else:
                    transit_in = "Origin Point"
                    arr_time = "N/A"

                events.append(
                    {
                        "title": node["location"].split(",")[0],  # Just grab the city
                        "start": start_str,
                        "end": end_str,
                        "allDay": True,
                        "backgroundColor": "rgba(34, 211, 238, 0.1)",
                        "borderColor": "#22d3ee",
                        "textColor": "#ffffff",
                        "extendedProps": {
                            "type": "stay",
                            "purpose": node.get("purpose", "TBD"),
                            "hostel": node.get("hostel", "TBD"),
                            "address": node.get("address", "TBD"),
                            "contact": node.get("contact", "TBD"),
                            "check_in": node.get("check_in", "TBD"),
                            "transit_in": transit_in,
                            "arrival": arr_time,
                        },
                    }
                )

        # 2. GENERATE TRANSIT EVENTS (Edges)
        for u, v, data in self.pipeline.edges(data=True):
            u_node = self.pipeline.nodes[u]
            v_node = self.pipeline.nodes[v]

            transit_start = u_node["end"]
            transit_end = v_node["start"]

            if transit_start:
                start_str = transit_start.strftime("%Y-%m-%d")
                # For transit, we DO add 1 day to the arrival date because it must span
                # the entirety of the travel days visually on the calendar.
                end_str = (transit_end + timedelta(days=1)).strftime("%Y-%m-%d")

                mode = data.get("mode", "Transit")
                events.append(
                    {
                        "title": f"Transit to {v_node['location'].split(',')[0]}",
                        "start": start_str,
                        "end": end_str,
                        "allDay": True,
                        "backgroundColor": "rgba(168, 85, 247, 0.15)",
                        "borderColor": "#a855f7",
                        "textColor": "#ffffff",
                        "extendedProps": {
                            "type": "transit",
                            "mode": mode,
                            "carrier": data.get("carrier", "TBD"),
                            "departure": data.get("departure", "TBD"),
                            "arrival": data.get("arrival", "TBD"),
                            "ref": data.get("ref", "TBD"),
                        },
                    }
                )

        # 3. GENERATE SHOW EVENTS (Using exact ISO timestamps)
        for show in self.shows_data:
            events.append(
                {
                    "title": show["event_name"],
                    "start": show["start_dt"].isoformat(),
                    "end": show["end_dt"].isoformat(),
                    "allDay": False,
                    "backgroundColor": "rgba(236, 72, 153, 0.15)",
                    "borderColor": "#ec4899",
                    "textColor": "#ffffff",
                    "extendedProps": {
                        "type": "show",
                        "venue": show.get("venue", "TBD"),
                        "lineup": show.get("lineup_notes", "TBD"),
                    },
                }
            )

        return {"events_json": json.dumps(events)}

    def plot_geographic_map(self):
        # Read the popup template into memory once before the loop
        template_path = PROJECT_ROOT / "src" / "templates" / "map_popup.html"
        with open(template_path, encoding="utf-8") as file:
            popup_template = Template(file.read())

        # 1. Revert to the high-contrast light base map
        m = folium.Map(location=[51.0, 4.0], zoom_start=5, tiles="CartoDB positron")

        # 2. Inject CSS to invert the base tiles via the pane container
        m.get_root().header.add_child(  # type: ignore[attr-defined]
            folium.Element("""
        <style>
            /* Apply inversion matrix to the entire tile container */
            .leaflet-tile-pane {
                -webkit-filter: invert(100%) hue-rotate(180deg) 
                    brightness(95%) contrast(90%);
                filter: invert(100%) hue-rotate(180deg) brightness(95%) contrast(90%);
            }
            
            /* Protostar Popup Overrides */
            .leaflet-popup-content-wrapper, .leaflet-popup-tip {
                background: #0a0f1f !important;
                border: 1px solid rgba(34, 211, 238, 0.14) !important;
                box-shadow: 0 10px 25px rgba(0,0,0,0.8) !important;
            }
            .leaflet-popup-close-button {
                color: #a9b7d0 !important;
            }
        </style>
        """)
        )

        sorted_nodes = list(nx.topological_sort(self.pipeline))
        path_coordinates = []

        for step_number, node_id in enumerate(sorted_nodes, start=1):
            node = self.pipeline.nodes[node_id]
            loc_string = node["location"]

            if loc_string in self.geo_cache:
                coords = self.geo_cache[loc_string]
            else:
                print(f"Cache miss for '{loc_string}'. Querying Nominatim...")
                location_data = self.geolocator.geocode(loc_string)
                time.sleep(1.1)  # Strict rate limit compliance
                if location_data:
                    coords = [location_data.latitude, location_data.longitude]  # type: ignore[union-attr]
                    self.geo_cache[loc_string] = coords
                    self._save_cache()
                else:
                    coords = None

            if coords:
                path_coordinates.append(coords)
                start_str = node["start"].strftime("%b %d")

                in_edges = list(self.pipeline.in_edges(node_id, data=True))
                if in_edges:
                    edge_data = in_edges[0][2]
                    transit_info = (
                        f"{edge_data.get('carrier')} ({edge_data.get('mode')})"
                    )
                    arr_time = (
                        f" | Arr: {edge_data.get('arrival')}"
                        if edge_data.get("arrival") != "TBD"
                        else ""
                    )
                else:
                    transit_info = "Origin Point"
                    arr_time = ""

                # Generate dynamic HTML block for shows if they exist on this node
                shows_val = node.get("shows_list", "")
                targeted_events_html = (
                    f"""
                <div style="margin-top:8px;">
                    <span style="font-size:0.75em; text-transform:uppercase; color:#ec4899; opacity: 0.8; font-weight:bold; display:block;">Targeted Events</span>
                    <span style="font-size:0.85em; color: #ffffff;">• {shows_val}</span>
                </div>"""  # noqa: E501
                    if shows_val
                    else ""
                )

                # Inject dynamic node attributes into the HTML
                popup_html = popup_template.safe_substitute(
                    step_number=step_number,
                    loc_string=loc_string,
                    start_str=start_str,
                    duration=node["duration"],
                    transit_info=transit_info,
                    arr_time=arr_time,
                    hostel=node.get("hostel", "TBD"),
                    address=node.get("address", "TBD"),
                    contact=node.get("contact", "TBD"),
                    check_in=node.get("check_in", "TBD"),
                    targeted_events_html=targeted_events_html,
                )

                folium.Marker(
                    location=coords,
                    popup=folium.Popup(popup_html, max_width=300),
                    tooltip=f"{step_number}. {loc_string}",
                    icon=BeautifyIcon(
                        number=step_number,
                        border_color="#22d3ee",
                        text_color="#22d3ee",
                        inner_icon_style="margin-top:0;",
                    ),  # type: ignore[arg-type]
                ).add_to(m)

        # Update the PolyLine color
        if len(path_coordinates) > 1:
            folium.PolyLine(
                path_coordinates,
                color="#22d3ee",
                weight=2.5,
                opacity=0.8,
                dash_array="8",
            ).add_to(m)

        return m
