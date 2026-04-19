from __future__ import annotations

from copy import deepcopy

from src.models.schemas import (
    ReportDefinition,
    Severity,
    StyleGuide,
    TransformationChange,
    TransformationPlan,
    WarningItem,
)


class StyleTransformationEngine:
    EDITABLE_STYLE_FIELDS = {
        "backgroundColor",
        "textColor",
        "cornerRadius",
        "titleAlignment",
        "showBorder",
        "alternatingRows",
        "legendPosition",
        "dataLabelColor",
    }

    def _apply_if_changed(self, plan: TransformationPlan, target: str, path: str, container: dict, key: str, new_value, risk_note: str | None = None) -> None:
        old_value = container.get(key)
        if old_value == new_value:
            return
        container[key] = new_value
        plan.changes.append(
            TransformationChange(target=target, path=path, old_value=old_value, new_value=new_value, risk_note=risk_note)
        )

    def apply_style_guide(self, report: ReportDefinition, style_guide: StyleGuide, dry_run: bool = True) -> tuple[ReportDefinition, TransformationPlan]:
        mutable = deepcopy(report)
        plan = TransformationPlan(report_id=report.report_id, workspace_id=report.workspace_id, dry_run=dry_run)

        for page in mutable.pages:
            if len(page.visuals) > style_guide.rules.max_visuals_per_page:
                plan.warnings.append(
                    WarningItem(
                        severity=Severity.WARNING,
                        code="max_visuals_exceeded",
                        message=f"Page {page.name} has {len(page.visuals)} visuals; max is {style_guide.rules.max_visuals_per_page}",
                        remediation="Split page visuals or increase style guide threshold.",
                    )
                )

            page.properties.setdefault("canvas", {})
            self._apply_if_changed(
                plan,
                target=f"page:{page.id}",
                path="canvas.padding",
                container=page.properties["canvas"],
                key="padding",
                new_value=style_guide.layout.page_padding,
            )

            for visual in page.visuals:
                visual.properties.setdefault("style", {})
                style = visual.properties["style"]

                self._apply_if_changed(
                    plan,
                    target=f"visual:{visual.id}",
                    path="style.backgroundColor",
                    container=style,
                    key="backgroundColor",
                    new_value=style_guide.theme.background_color,
                )
                self._apply_if_changed(
                    plan,
                    target=f"visual:{visual.id}",
                    path="style.textColor",
                    container=style,
                    key="textColor",
                    new_value=style_guide.theme.text_color,
                )
                self._apply_if_changed(
                    plan,
                    target=f"visual:{visual.id}",
                    path="style.cornerRadius",
                    container=style,
                    key="cornerRadius",
                    new_value=style_guide.layout.corner_radius,
                )

                type_rules = style_guide.visual_rules.get(visual.visual_type, {})
                for rule_key, rule_value in type_rules.items():
                    if rule_key not in self.EDITABLE_STYLE_FIELDS:
                        plan.warnings.append(
                            WarningItem(
                                severity=Severity.INFO,
                                code="non_editable_rule_skipped",
                                message=f"Skipped unsupported style rule '{rule_key}' for visual {visual.id}",
                                remediation="Add deterministic mapping before applying this rule.",
                            )
                        )
                        continue
                    self._apply_if_changed(
                        plan,
                        target=f"visual:{visual.id}",
                        path=f"style.{rule_key}",
                        container=style,
                        key=rule_key,
                        new_value=rule_value,
                    )

                if visual.visual_type.startswith("custom") and not style_guide.rules.allow_custom_visuals:
                    plan.warnings.append(
                        WarningItem(
                            severity=Severity.BLOCKER,
                            code="custom_visual_disallowed",
                            message=f"Custom visual {visual.id} violates style guide policy.",
                            remediation="Replace custom visual or set allowCustomVisuals=true.",
                        )
                    )

        if dry_run:
            return report, plan
        return mutable, plan
