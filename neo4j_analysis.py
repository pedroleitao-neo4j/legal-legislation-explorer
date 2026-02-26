from neo4j import GraphDatabase, Result
import pandas as pd
from playwright.async_api import async_playwright
import os


class Neo4jAnalysis:
    """Helper class to consolidate Neo4j query and visualization functionality."""

    def __init__(self, uri, user, password, database):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database

    def close(self):
        """Close the Neo4j driver connection."""
        self.driver.close()

    def run_query(self, query, params=None):
        """Execute a Cypher query and return results as a list of dictionaries."""
        with self.driver.session(database=self.database) as session:
            result = session.run(query, params or {})
            return [record.data() for record in result]

    def run_query_df(self, query, params=None):
        """Execute a Cypher query and return results as a pandas DataFrame."""
        records = self.run_query(query, params)
        return pd.DataFrame(records)

    def run_query_single(self, query, params=None):
        """Execute a Cypher query and return a single record."""
        with self.driver.session(database=self.database) as session:
            result = session.run(query, params or {})
            return result.single()

    def run_query_viz(self, query, params=None):
        result = self.driver.execute_query(
            query,
            parameters_=params or {},
            database_=self.database,
            result_transformer_=Result.graph,
        )
        return result

    async def capture_graph_to_png(
        self,
        html_content,
        output_path,
        scale=2,
        width=1920,
        height=1080,
        html_file=None,
    ):
        """
        Capture a graph visualization to a PNG file with configurable resolution.

        Args:
            html_content: The HTML content from neo4j_viz render()
            output_path: Path to save the PNG file
            scale: Device scale factor for higher resolution (default: 2 for 2x/Retina quality)
            width: Viewport width in pixels (default: 1920)
            height: Viewport height in pixels (default: 1080)
        """
        # Inject CSS to center the graph and fill the viewport
        centering_css = """
        <style>
            html, body {
                margin: 0;
                padding: 0;
                width: 100%;
                height: 100%;
                overflow: hidden;
            }
            body > div, canvas, #graph-container, .nvg-container {
                width: 100% !important;
                height: 100% !important;
                display: flex;
                justify-content: center;
                align-items: center;
            }
        </style>
        """
        # If html_file is provided, load the HTML content from the file instead of using the provided html_content
        if html_file:
            with open(html_file, "r", encoding="utf-8") as f:
                html_data = f.read()
        else:
            html_data = html_content.data

        # Inject centering CSS into the HTML content
        if not html_file:
            if "<head>" in html_data:
                html_data = html_data.replace("<head>", f"<head>{centering_css}")
            else:
                html_data = centering_css + html_data

        # Save content to a temporary HTML file
        if not html_file:
            with open("remove.html", "w", encoding="utf-8") as f:
                f.write(html_data)
        async with async_playwright() as p:
            # Launch a headless browser
            browser = await p.chromium.launch()
            # Create page with higher resolution viewport and device scale factor
            page = await browser.new_page(
                viewport={"width": width, "height": height}, device_scale_factor=scale
            )

            # Load the local HTML file
            abs_path = (
                f"file://{os.path.abspath('remove.html')}"
                if not html_file
                else f"file://{os.path.abspath(html_file)}"
            )
            await page.goto(abs_path)

            # Wait for the force-directed layout to stabilize
            await page.wait_for_timeout(8000)

            # Take screenshot of just the viewport (not full_page) for centered output
            await page.screenshot(path=output_path, full_page=False)
            await browser.close()
        # Clean up the temporary file
        os.remove("remove.html") if not html_file else None
        
    def set_caption_by_label(self,VG, label_to_property):
        for node in VG.nodes:
            labels = node.properties.get("labels", [])
            for label, prop in label_to_property.items():
                if label in labels:
                    node.caption = node.properties.get(prop)
                    break

    def verify_connection(self):
        """Verify the Neo4j connection is working."""
        record = self.run_query_single("RETURN 1 AS test")
        return record and record["test"] == 1