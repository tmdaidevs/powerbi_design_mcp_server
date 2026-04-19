from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.config.settings import settings
from src.diffing.diff_engine import DiffEngine
from src.fabric_client.client import FabricApiClient, FabricApiError
from src.models.schemas import (
    MCPErrorCode,
    ReportDefinition,
    ReportPart,
    Severity,
    StyleGuide,
    ToolResponse,
    WarningItem,
)
from src.parser.definition_parser import ReportDefinitionParser
from src.transformations.style_engine import StyleTransformationEngine
from src.utils.scoring import score_modernization
from src.validation.validator import ReportValidator


class ReportModernizationService:
    def __init__(
        self,
        api_client: FabricApiClient | None = None,
        parser: ReportDefinitionParser | None = None,
        transformer: StyleTransformationEngine | None = None,
        validator: ReportValidator | None = None,
        diff_engine: DiffEngine | None = None,
    ) -> None:
        self.api_client = api_client or FabricApiClient()
        self.parser = parser or ReportDefinitionParser()
        self.transformer = transformer or StyleTransformationEngine()
        self.validator = validator or ReportValidator()
        self.diff_engine = diff_engine or DiffEngine()

    def _load_report(self, workspace_id: str, report_id: str) -> ReportDefinition:
        raw = self.api_client.get_report_definition(workspace_id, report_id)
        if raw.get("status") == "pending":
            location = raw.get("location")
            if not location:
                raise RuntimeError(f"{MCPErrorCode.ASYNC_OPERATION_PENDING.value}: operation location missing")
            operation = self.api_client.wait_for_operation(location)
            if operation.status.lower() != "succeeded":
                raise RuntimeError(f"{MCPErrorCode.ASYNC_OPERATION_PENDING.value}: operation status {operation.status}")
            raw = operation.payload or {}
        return self.parser.parse(workspace_id, report_id, raw)

    def _validate_report_or_block(self, report: ReportDefinition) -> tuple[list[WarningItem], list[WarningItem]]:
        validation = self.validator.validate(report)
        warnings = [issue for issue in validation.issues if issue.severity != Severity.BLOCKER]
        blockers = [issue for issue in validation.issues if issue.severity == Severity.BLOCKER]
        return warnings, blockers

    def _resolve_page(self, report: ReportDefinition, page_id_or_name: str):
        return next((p for p in report.pages if p.id == page_id_or_name or p.name == page_id_or_name), None)

    def _resolve_visual(self, page, visual_id_or_name: str):
        return next((v for v in page.visuals if v.id == visual_id_or_name or v.name == visual_id_or_name), None)

    def _report_to_definition_parts(self, report: ReportDefinition) -> dict[str, Any]:
        out_parts: list[dict[str, Any]] = []
        page_map = {p.name: p for p in report.pages}

        for part in report.parts:
            payload = deepcopy(part.payload)
            if isinstance(payload, dict) and part.name in page_map:
                page = page_map[part.name]
                payload.update(page.properties)
                payload["visuals"] = []
                for visual in page.visuals:
                    merged = deepcopy(visual.raw)
                    merged["config"] = visual.properties
                    payload["visuals"].append(merged)
            out_parts.append({"name": part.name, "path": part.path, "contentType": part.content_type, "payload": payload})

        return {"definition": {"format": report.format.value, "parts": out_parts}, "metadata": report.metadata}

    def analyze_report_structure(self, workspace_id: str, report_id: str) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        warnings, blockers = self._validate_report_or_block(report)
        score = score_modernization(report)
        return ToolResponse(
            success=True,
            summary="Report structure analyzed",
            data={
                "report": {
                    "reportId": report.report_id,
                    "workspaceId": report.workspace_id,
                    "format": report.format.value,
                    "pageCount": len(report.pages),
                    "visualCount": sum(len(p.visuals) for p in report.pages),
                    "bookmarkCount": len(report.bookmarks),
                    "staticResourceCount": len(report.static_resources),
                },
                "modernizationScore": score.model_dump(),
            },
            warnings=warnings,
            blockers=blockers,
            next_actions=["Run get_report_pages for detailed page inventory", "Run apply_style_guide with dry_run=true"],
        )

    def apply_style_guide(self, workspace_id: str, report_id: str, style_guide_payload: dict[str, Any], dry_run: bool = True) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        style_guide = StyleGuide.model_validate(style_guide_payload)

        warnings, blockers = self._validate_report_or_block(report)
        if blockers:
            return ToolResponse(
                success=False,
                summary="Style guide application blocked due to validation blockers",
                blockers=blockers,
                warnings=warnings,
                next_actions=["Resolve blockers", "Retry in dry-run mode after conversion/remediation"],
            )

        transformed, plan = self.transformer.apply_style_guide(report, style_guide, dry_run=dry_run)
        diff = self.diff_engine.diff_reports(report, transformed)
        data = {
            "dryRun": dry_run,
            "changeCount": len(plan.changes),
            "plan": plan.model_dump(by_alias=True),
            "diff": diff.model_dump(),
        }

        if not dry_run:
            data["definitionParts"] = self._report_to_definition_parts(transformed)

        return ToolResponse(
            success=True,
            summary="Style guide evaluation completed",
            data=data,
            warnings=warnings + plan.warnings,
            next_actions=["Review diff", "Run update_report_definition with confirm=true to persist"],
        )

    def backup_report_definition(self, workspace_id: str, report_id: str) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        definition = report.model_dump(mode="json")
        backup_dir = Path(settings.backup_directory)
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"{workspace_id}_{report_id}_{timestamp}.json"
        backup_path.write_text(json.dumps(definition, indent=2), encoding="utf-8")
        return ToolResponse(
            success=True,
            summary="Backup snapshot prepared",
            data={
                "workspaceId": workspace_id,
                "reportId": report_id,
                "backupPath": str(backup_path),
            },
            next_actions=["Store backup file in immutable object storage before writeback"],
        )

    def update_report_definition(self, workspace_id: str, report_id: str, definition_parts: dict[str, Any], confirm: bool = False) -> ToolResponse:
        if not confirm:
            return ToolResponse(
                success=False,
                summary="Writeback blocked because confirm=false",
                blockers=[
                    WarningItem(
                        severity=Severity.BLOCKER,
                        code=MCPErrorCode.CORRUPTED_PAYLOAD_RISK.value,
                        message="Explicit confirmation required before update.",
                        remediation="Set confirm=true after reviewing dry-run diff and validation results.",
                    )
                ],
                next_actions=["Run backup_report_definition", "Run validate_report_definition", "Retry with confirm=true"],
            )

        try:
            result = self.api_client.update_report_definition(workspace_id, report_id, definition_parts)
            if result.get("status") == "pending" and result.get("location"):
                state = self.api_client.wait_for_operation(result["location"])
                result = {"status": state.status, "payload": state.payload}
        except FabricApiError as exc:
            return ToolResponse(
                success=False,
                summary="Update request failed",
                blockers=[
                    WarningItem(
                        severity=Severity.BLOCKER,
                        code=exc.code.value,
                        message=str(exc),
                        remediation="Review API permissions, payload validity, and retry policy.",
                    )
                ],
                data={"statusCode": exc.status_code, "payload": exc.payload},
            )

        return ToolResponse(
            success=True,
            summary="Update request completed",
            data=result,
            next_actions=["Re-run analyze_report_structure to verify post-update integrity"],
        )

    def preview_changes(self, before_definition: dict[str, Any], after_definition: dict[str, Any]) -> ToolResponse:
        diff = self.diff_engine.diff_parts(before_definition, after_definition)
        return ToolResponse(success=True, summary="Definition diff generated", data=diff.model_dump(), next_actions=["Review changed parts and risk notes"])

    def validate_report(self, workspace_id: str, report_id: str) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        warnings, blockers = self._validate_report_or_block(report)
        return ToolResponse(
            success=not blockers,
            summary="Validation completed",
            data={"valid": not blockers},
            warnings=warnings,
            blockers=blockers,
            next_actions=["Fix blockers before writeback"] if blockers else ["Proceed with dry-run transformations"],
        )

    def list_workspaces(self) -> ToolResponse:
        return ToolResponse(success=True, summary="Workspaces listed", data={"workspaces": self.api_client.list_workspaces()})

    def list_reports(self, workspace_id: str) -> ToolResponse:
        return ToolResponse(success=True, summary="Reports listed", data={"workspaceId": workspace_id, "reports": self.api_client.list_reports(workspace_id)})

    def get_report_metadata(self, workspace_id: str, report_id: str) -> ToolResponse:
        metadata = self.api_client.get_report_metadata(workspace_id, report_id)
        return ToolResponse(success=True, summary="Report metadata retrieved", data=metadata)

    def get_report_pages(self, workspace_id: str, report_id: str) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        return ToolResponse(success=True, summary="Report pages retrieved", data={"pages": [p.model_dump() for p in report.pages]})

    def get_page_visuals(self, workspace_id: str, report_id: str, page_id_or_name: str) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        page = self._resolve_page(report, page_id_or_name)
        if not page:
            return ToolResponse(success=False, summary="Page not found", blockers=[WarningItem(severity=Severity.BLOCKER, code="page_not_found", message=page_id_or_name, remediation="Use get_report_pages to list valid identifiers")])
        return ToolResponse(success=True, summary="Page visuals retrieved", data={"page": page.name, "visuals": [v.model_dump() for v in page.visuals]})

    def get_report_assets(self, workspace_id: str, report_id: str) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        return ToolResponse(success=True, summary="Report assets retrieved", data={"bookmarks": [b.model_dump() for b in report.bookmarks], "staticResources": [s.model_dump() for s in report.static_resources]})

    def score_modernization_readiness(self, workspace_id: str, report_id: str) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        return ToolResponse(success=True, summary="Modernization readiness scored", data=score_modernization(report).model_dump())

    def patch_report_properties(self, workspace_id: str, report_id: str, patch: dict[str, Any], dry_run: bool = True) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        before = deepcopy(report.metadata)
        after = deepcopy(report.metadata)
        after.update(patch)
        if dry_run:
            return self.preview_changes(before, after)
        report.metadata = after
        return ToolResponse(success=True, summary="Report patch planned", data={"definitionParts": self._report_to_definition_parts(report)})

    def patch_page_properties(self, workspace_id: str, report_id: str, page_id_or_name: str, patch: dict[str, Any], dry_run: bool = True) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        page = self._resolve_page(report, page_id_or_name)
        if not page:
            return ToolResponse(success=False, summary="Page not found", blockers=[WarningItem(severity=Severity.BLOCKER, code="page_not_found", message=page_id_or_name, remediation="Use get_report_pages to list valid page IDs")])

        before = deepcopy(page.properties)
        after = deepcopy(page.properties)
        after.update(patch)

        if dry_run:
            diff = self.diff_engine.diff_parts(before, after)
            return ToolResponse(success=True, summary="Page patch dry-run generated", data={"dryRun": True, "diff": diff.model_dump()})

        page.properties = after
        return ToolResponse(success=True, summary="Page patch applied", data={"definitionParts": self._report_to_definition_parts(report), "dryRun": False})

    def patch_visual_properties(
        self,
        workspace_id: str,
        report_id: str,
        page_id_or_name: str,
        visual_id_or_name: str,
        patch: dict[str, Any],
        dry_run: bool = True,
    ) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        page = self._resolve_page(report, page_id_or_name)
        if not page:
            return ToolResponse(success=False, summary="Page not found", blockers=[WarningItem(severity=Severity.BLOCKER, code="page_not_found", message=page_id_or_name, remediation="Use get_report_pages to list valid page IDs")])

        visual = self._resolve_visual(page, visual_id_or_name)
        if not visual:
            return ToolResponse(success=False, summary="Visual not found", blockers=[WarningItem(severity=Severity.BLOCKER, code="visual_not_found", message=visual_id_or_name, remediation="Use get_page_visuals to list visual IDs")])

        before = deepcopy(visual.properties)
        after = deepcopy(visual.properties)
        after.update(patch)

        if dry_run:
            diff = self.diff_engine.diff_parts(before, after)
            return ToolResponse(success=True, summary="Visual patch dry-run generated", data={"dryRun": True, "diff": diff.model_dump()})

        visual.properties = after
        return ToolResponse(success=True, summary="Visual patch applied", data={"definitionParts": self._report_to_definition_parts(report), "dryRun": False})

    def replace_theme_resource(self, workspace_id: str, report_id: str, theme_payload: dict[str, Any], dry_run: bool = True) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)
        target_part: ReportPart | None = None
        for part in report.parts:
            if "theme" in part.path.lower() or "theme" in part.name.lower():
                target_part = part
                break

        if not target_part:
            return ToolResponse(success=False, summary="Theme resource not found", blockers=[WarningItem(severity=Severity.BLOCKER, code="theme_resource_missing", message="No theme part discovered", remediation="Use get_report_assets to inspect available resources")])

        before = deepcopy(target_part.payload)
        after = deepcopy(theme_payload)

        if dry_run:
            return ToolResponse(success=True, summary="Theme replacement dry-run generated", data={"dryRun": True, "diff": self.diff_engine.diff_parts(before, after).model_dump()})

        target_part.payload = after
        return ToolResponse(success=True, summary="Theme replaced", data={"dryRun": False, "definitionParts": self._report_to_definition_parts(report)})

    def extract_style_guide_from_report(self, workspace_id: str, report_id: str, include_visual_rules: bool = True) -> ToolResponse:
        report = self._load_report(workspace_id, report_id)

        style_background: dict[str, int] = {}
        style_text: dict[str, int] = {}
        style_corner_radius: dict[int, int] = {}
        title_fonts: dict[str, int] = {}
        body_fonts: dict[str, int] = {}
        title_sizes: dict[int, int] = {}
        body_sizes: dict[int, int] = {}
        visual_rules: dict[str, dict[str, Any]] = {}

        def bump(counter: dict[Any, int], value: Any) -> None:
            if value is None:
                return
            counter[value] = counter.get(value, 0) + 1

        for page in report.pages:
            for visual in page.visuals:
                style = visual.properties.get("style", {}) if isinstance(visual.properties, dict) else {}
                bump(style_background, style.get("backgroundColor"))
                bump(style_text, style.get("textColor"))
                bump(style_corner_radius, style.get("cornerRadius"))
                bump(title_fonts, style.get("titleFontFamily"))
                bump(body_fonts, style.get("bodyFontFamily"))
                bump(title_sizes, style.get("titleFontSize"))
                bump(body_sizes, style.get("bodyFontSize"))

                if include_visual_rules:
                    candidate = {
                        key: value
                        for key, value in style.items()
                        if key in {"titleAlignment", "showBorder", "alternatingRows", "legendPosition", "dataLabelColor"}
                    }
                    if candidate:
                        visual_rules.setdefault(visual.visual_type, {}).update(candidate)

        def most_common(counter: dict[Any, int], fallback: Any) -> Any:
            if not counter:
                return fallback
            return sorted(counter.items(), key=lambda item: item[1], reverse=True)[0][0]

        extracted = {
            "theme": {
                "primaryColor": report.metadata.get("theme", {}).get("primaryColor", "#0078D4"),
                "backgroundColor": most_common(style_background, "#FFFFFF"),
                "textColor": most_common(style_text, "#1F1F1F"),
            },
            "typography": {
                "titleFontFamily": most_common(title_fonts, "Segoe UI Semibold"),
                "bodyFontFamily": most_common(body_fonts, "Segoe UI"),
                "titleFontSize": most_common(title_sizes, 16),
                "bodyFontSize": most_common(body_sizes, 11),
            },
            "layout": {
                "pagePadding": 16,
                "visualSpacing": 12,
                "cornerRadius": most_common(style_corner_radius, 8),
            },
            "rules": {
                "maxVisualsPerPage": max(1, max((len(p.visuals) for p in report.pages), default=6)),
                "allowCustomVisuals": True,
                "enforceTopRowKpis": False,
            },
            "visualRules": visual_rules if include_visual_rules else {},
        }

        return ToolResponse(
            success=True,
            summary="Style guide extracted from report",
            data={
                "workspaceId": workspace_id,
                "reportId": report_id,
                "styleGuide": extracted,
                "sampling": {
                    "pageCount": len(report.pages),
                    "visualCount": sum(len(p.visuals) for p in report.pages),
                },
            },
            next_actions=[
                "Review and harden extracted style guide before bulk rollout",
                "Use apply_style_guide with dry_run=true on target reports",
            ],
        )

    def bulk_apply_style_guide(
        self,
        workspace_id: str,
        report_ids: list[str],
        style_guide_payload: dict[str, Any],
        dry_run: bool = True,
        continue_on_error: bool = True,
    ) -> ToolResponse:
        results: list[dict[str, Any]] = []

        def run_one(rid: str) -> dict[str, Any]:
            try:
                return {"reportId": rid, "result": self.apply_style_guide(workspace_id, rid, style_guide_payload, dry_run=dry_run).model_dump(mode="json")}
            except Exception as exc:  # noqa: BLE001
                if not continue_on_error:
                    raise
                return {
                    "reportId": rid,
                    "result": ToolResponse(
                        success=False,
                        summary="Bulk apply failed",
                        blockers=[WarningItem(severity=Severity.BLOCKER, code="bulk_item_failed", message=str(exc), remediation="Inspect report-specific payload and retry")],
                    ).model_dump(mode="json"),
                }

        with ThreadPoolExecutor(max_workers=max(1, settings.bulk_max_workers)) as pool:
            futures = {pool.submit(run_one, rid): rid for rid in report_ids}
            for future in as_completed(futures):
                results.append(future.result())

        return ToolResponse(
            success=all(r["result"].get("success", False) for r in results),
            summary="Bulk style guide run completed",
            data={"workspaceId": workspace_id, "results": sorted(results, key=lambda r: r["reportId"]), "dryRun": dry_run},
            next_actions=["Review per-report blockers/warnings before persistence"],
        )
