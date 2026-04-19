from __future__ import annotations

import json
from typing import Any

from src.models.schemas import (
    BookmarkDefinition,
    PageDefinition,
    ReportDefinition,
    ReportFormat,
    ReportPart,
    StaticResource,
    VisualDefinition,
)


class ReportDefinitionParser:
    def _normalize_visual_properties(self, visual_raw: dict[str, Any]) -> dict[str, Any]:
        config = visual_raw.get("config", {})
        if isinstance(config, str):
            try:
                return json.loads(config)
            except json.JSONDecodeError:
                return {"rawConfig": config, "parseError": "visual_config_not_json"}
        if isinstance(config, dict):
            return config
        return {"rawConfig": config}

    def parse(self, workspace_id: str, report_id: str, payload: dict[str, Any]) -> ReportDefinition:
        definition = payload.get("definition", {})
        parts_raw = definition.get("parts", [])
        parts = [
            ReportPart(
                name=p.get("name", "unknown"),
                path=p.get("path", p.get("name", "unknown")),
                content_type=p.get("contentType", "application/json"),
                payload=p.get("payload", {}),
            )
            for p in parts_raw
        ]

        format_value = definition.get("format", "Unknown")
        report_format = ReportFormat(format_value) if format_value in ReportFormat._value2member_map_ else ReportFormat.UNKNOWN

        pages: list[PageDefinition] = []
        bookmarks: list[BookmarkDefinition] = []
        static_resources: list[StaticResource] = []
        unsupported_artifacts: list[str] = []

        for idx, part in enumerate(parts):
            path_lower = part.path.lower()

            if "/pages/" in f"/{path_lower}" and isinstance(part.payload, dict):
                visuals_raw = part.payload.get("visuals", [])
                visuals = [
                    VisualDefinition(
                        id=v.get("id", v.get("name", f"visual_{n}")),
                        name=v.get("name"),
                        visual_type=v.get("type", "unknown"),
                        page_id=part.payload.get("id", part.name),
                        properties=self._normalize_visual_properties(v),
                        raw=v,
                    )
                    for n, v in enumerate(visuals_raw)
                ]
                pages.append(
                    PageDefinition(
                        id=part.payload.get("id", f"page_{idx}"),
                        name=part.payload.get("name", part.name),
                        display_name=part.payload.get("displayName"),
                        order=part.payload.get("ordinal", idx),
                        visuals=visuals,
                        properties={k: v for k, v in part.payload.items() if k != "visuals"},
                        raw=part.payload,
                    )
                )
                continue

            if "bookmark" in path_lower and isinstance(part.payload, dict):
                bookmarks.append(
                    BookmarkDefinition(
                        id=part.payload.get("id", part.name),
                        name=part.payload.get("name", part.name),
                        raw=part.payload,
                    )
                )
                continue

            if any(x in path_lower for x in ("staticresources", "resources", "themes")):
                static_resources.append(
                    StaticResource(name=part.name, resource_type=part.content_type, raw={"path": part.path, "payload": part.payload})
                )

            if any(x in path_lower for x in ("mobilestate", "legacy", "customvisualstate")):
                unsupported_artifacts.append(part.path)

        if not pages:
            unsupported_artifacts.append("missing_pages")

        return ReportDefinition(
            report_id=report_id,
            workspace_id=workspace_id,
            format=report_format,
            metadata=payload.get("metadata", {}),
            parts=parts,
            pages=pages,
            bookmarks=bookmarks,
            static_resources=static_resources,
            unsupported_artifacts=unsupported_artifacts,
        )
