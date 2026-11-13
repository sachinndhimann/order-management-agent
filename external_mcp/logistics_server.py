"""A THIRD-PARTY MCP server (simulated logistics provider) - Chapter 4/6.

It lives outside our domain (own process, own container, own auth: a static API
key as bearer token) to demonstrate integrating an MCP server we do not own.
Our agent discovers its tools at runtime exactly like it does for our own server.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import BaseModel, Field


class Tracking(BaseModel):
    tracking_number: str
    carrier: str
    status: str
    last_location: str


class Estimate(BaseModel):
    postal_code: str
    days: int
    service: str


def create_logistics_server(api_key: str | None = None) -> FastMCP:
    key = api_key or os.getenv("EXTERNAL_MCP_API_KEY", "logistics-dev-key")
    mcp = FastMCP("acme-logistics",
                  auth=StaticTokenVerifier({key: {"client_id": "order-agent", "scopes": ["logistics"]}}),
                  mask_error_details=True)

    @mcp.tool(annotations={"readOnlyHint": True})
    def track_shipment(tracking_number: Annotated[str, Field(pattern=r"^[A-Z]{2}\d{6,12}$",
                       description="Carrier tracking number, e.g. AC12345678")]) -> Tracking:
        """Look up the live status of a shipment by tracking number."""
        if tracking_number.endswith("0"):
            raise ToolError("Tracking number not found")
        h = int(hashlib.sha1(tracking_number.encode()).hexdigest(), 16)
        status = ["label_created", "in_transit", "out_for_delivery", "delivered"][h % 4]
        return Tracking(tracking_number=tracking_number, carrier="ACME Freight", status=status,
                        last_location=["Oakland, CA", "Reno, NV", "Denver, CO"][h % 3])

    @mcp.tool(annotations={"readOnlyHint": True})
    def estimate_delivery(postal_code: Annotated[str, Field(min_length=3, max_length=10)]) -> Estimate:
        """Estimate delivery time in days for a destination postal code."""
        days = 2 + int(hashlib.md5(postal_code.encode()).hexdigest(), 16) % 5
        return Estimate(postal_code=postal_code, days=days, service="ground" if days > 3 else "express")

    return mcp


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8002)
    a = ap.parse_args()
    create_logistics_server().run(transport="http", host="0.0.0.0", port=a.port, path="/mcp")
