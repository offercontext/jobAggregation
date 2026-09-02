from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvidenceEntry:
    evidence_id: str
    source: str
    path: str
    excerpt: str

    def public_ref(self) -> dict[str, str]:
        return {"source": self.source, "path": self.path, "excerpt": self.excerpt}

    def provider_value(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "path": self.path,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class TemplateOption:
    template_id: str
    section: str
    evidence_ref_ids: tuple[str, ...]
    purpose: str

    def provider_value(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "section": self.section,
            "evidence_ref_ids": list(self.evidence_ref_ids),
            "purpose": self.purpose,
        }


class TemplateContractError(ValueError):
    def __init__(self, message: str, category: str) -> None:
        super().__init__(message)
        self.category = category


_FIXED_EVIDENCE: tuple[tuple[str, str], ...] = (
    ("company_name", "offer.company_name"),
    ("position_name", "offer.position_name"),
    ("status", "offer.status"),
    ("base_monthly", "offer.base_monthly"),
    ("months_per_year", "offer.months_per_year"),
    ("signing_bonus", "offer.signing_bonus"),
    ("equity", "offer.equity"),
    ("perks", "offer.perks"),
    ("deadline", "offer.deadline"),
    ("notes", "offer.notes"),
)
_BRIEF_EVIDENCE: tuple[tuple[str, str], ...] = (
    ("goal", "brief.goal"),
    ("concerns", "brief.concerns"),
    ("scenario", "brief.scenario"),
)
_DIMENSION_ID_RE = re.compile(r"^offer\.dimension\.(dimension_[0-9]{3})$")

_STATIC_OPTIONS: tuple[TemplateOption, ...] = (
    TemplateOption(
        "goal_focus_request",
        "communication_goals",
        ("brief.goal",),
        "把本次沟通聚焦为一个明确诉求",
    ),
    TemplateOption(
        "goal_interest_then_request",
        "communication_goals",
        ("offer.company_name", "offer.position_name", "brief.goal"),
        "先说明加入意愿，再提出明确诉求",
    ),
    TemplateOption(
        "ask_current_compensation_structure",
        "clarification_questions",
        ("offer.base_monthly", "offer.months_per_year"),
        "确认固定月薪和计薪月数的组成与发放口径",
    ),
    TemplateOption(
        "ask_request_flexibility",
        "clarification_questions",
        ("brief.goal",),
        "询问当前方案围绕本次目标还有哪些可讨论空间",
    ),
    TemplateOption(
        "ask_concern_details",
        "clarification_questions",
        ("brief.concerns",),
        "把用户顾虑改写成需要对方澄清的具体问题",
    ),
    TemplateOption(
        "ask_offer_validity_after_request",
        "clarification_questions",
        ("brief.concerns",),
        "当用户担心谈薪影响已发 Offer 时，确认现有方案是否继续有效",
    ),
    TemplateOption(
        "ask_signing_bonus_terms",
        "clarification_questions",
        ("offer.signing_bonus",),
        "确认签字费记录及其条件",
    ),
    TemplateOption(
        "ask_equity_terms",
        "clarification_questions",
        ("offer.equity",),
        "确认股权记录的具体口径",
    ),
    TemplateOption(
        "ask_benefit_terms",
        "clarification_questions",
        ("offer.perks",),
        "确认福利记录的具体口径",
    ),
    TemplateOption(
        "ask_decision_deadline",
        "clarification_questions",
        ("offer.deadline",),
        "确认决策截止时间与回复节奏",
    ),
    TemplateOption(
        "say_interest_and_request",
        "talking_points",
        ("offer.company_name", "offer.position_name", "brief.goal"),
        "生成可直接参考的意愿与诉求表达",
    ),
    TemplateOption(
        "say_current_offer_and_request",
        "talking_points",
        ("offer.base_monthly", "offer.months_per_year", "brief.goal"),
        "基于当前固定薪酬口径提出诉求",
    ),
    TemplateOption(
        "say_concern_and_request",
        "talking_points",
        ("brief.concerns", "brief.goal"),
        "在表达诉求时一并提出需要澄清的顾虑",
    ),
    TemplateOption(
        "say_request_and_preserve_offer",
        "talking_points",
        ("brief.goal", "brief.concerns"),
        "当用户担心谈薪影响 Offer 时，同时提出诉求并确认现有方案仍有效",
    ),
    TemplateOption(
        "say_scenario_opening",
        "talking_points",
        ("brief.scenario", "brief.goal"),
        "根据本次沟通场景生成开场表达",
    ),
    TemplateOption(
        "check_current_compensation",
        "preparation_checks",
        ("offer.base_monthly", "offer.months_per_year"),
        "沟通前核对当前固定薪酬记录",
    ),
    TemplateOption(
        "check_goal_and_concern",
        "preparation_checks",
        ("brief.goal", "brief.concerns"),
        "提前写下目标、底线和需要澄清的顾虑",
    ),
    TemplateOption(
        "check_decision_deadline",
        "preparation_checks",
        ("offer.deadline",),
        "沟通前核对决策截止时间",
    ),
    TemplateOption(
        "check_written_follow_up",
        "preparation_checks",
        ("offer.company_name", "offer.position_name"),
        "如条件变化，核对更新后的书面 Offer",
    ),
)
_DYNAMIC_TEMPLATES = {
    "ask_comparison_dimension": "clarification_questions",
    "check_comparison_dimension": "preparation_checks",
}
TEMPLATE_IDS = tuple(
    sorted({option.template_id for option in _STATIC_OPTIONS} | set(_DYNAMIC_TEMPLATES))
)
TEMPLATE_SECTIONS = {
    **{option.template_id: option.section for option in _STATIC_OPTIONS},
    **_DYNAMIC_TEMPLATES,
}


def build_evidence_catalog(snapshot: dict[str, Any]) -> tuple[EvidenceEntry, ...]:
    entries: list[EvidenceEntry] = []
    offer = snapshot.get("offer_snapshot")
    if isinstance(offer, dict):
        for field, evidence_id in _FIXED_EVIDENCE:
            entry = _entry(
                evidence_id,
                "offer_snapshot",
                f"/offer_snapshot/{field}",
                offer.get(field),
            )
            if entry is not None:
                entries.append(entry)
        dimensions = offer.get("dimensions")
        if isinstance(dimensions, list):
            for dimension in dimensions:
                if not isinstance(dimension, dict):
                    continue
                path_id = dimension.get("path_id")
                if not isinstance(path_id, str) or not re.fullmatch(r"dimension_[0-9]{3}", path_id):
                    continue
                entry = _entry(
                    f"offer.dimension.{path_id}",
                    "offer_snapshot",
                    f"/offer_snapshot/dimensions/{path_id}/value_text",
                    dimension.get("value_text"),
                )
                if entry is not None:
                    entries.append(entry)
    brief = snapshot.get("user_brief")
    if isinstance(brief, dict):
        for field, evidence_id in _BRIEF_EVIDENCE:
            entry = _entry(
                evidence_id,
                "user_brief",
                f"/user_brief/{field}",
                brief.get(field),
            )
            if entry is not None:
                entries.append(entry)
    return tuple(entries)


def build_template_catalog(snapshot: dict[str, Any]) -> tuple[TemplateOption, ...]:
    evidence_ids = {entry.evidence_id for entry in build_evidence_catalog(snapshot)}
    available = [
        option for option in _STATIC_OPTIONS if set(option.evidence_ref_ids).issubset(evidence_ids)
    ]
    for evidence_id in sorted(evidence_ids):
        if _DIMENSION_ID_RE.fullmatch(evidence_id) is None:
            continue
        available.extend(
            (
                TemplateOption(
                    "ask_comparison_dimension",
                    "clarification_questions",
                    (evidence_id,),
                    "确认一个自定义比较维度的具体口径",
                ),
                TemplateOption(
                    "check_comparison_dimension",
                    "preparation_checks",
                    (evidence_id,),
                    "沟通前核对一个自定义比较维度",
                ),
            )
        )
    return tuple(available)


def render_template(
    *,
    section: str,
    template_id: str,
    evidence_ref_ids: list[str],
    snapshot: dict[str, Any],
) -> tuple[str, str, list[dict[str, str]]]:
    if template_id not in TEMPLATE_SECTIONS:
        raise TemplateContractError("template is unknown", "invalid_field_type")
    if TEMPLATE_SECTIONS[template_id] != section:
        raise TemplateContractError(
            "template belongs to another section", "template_section_mismatch"
        )
    entries = {entry.evidence_id: entry for entry in build_evidence_catalog(snapshot)}
    for evidence_id in evidence_ref_ids:
        if evidence_id not in entries:
            raise TemplateContractError("evidence id is unknown", "unknown_evidence_ref")
    matching_option = next(
        (
            option
            for option in build_template_catalog(snapshot)
            if option.template_id == template_id
            and option.section == section
            and list(option.evidence_ref_ids) == evidence_ref_ids
        ),
        None,
    )
    if matching_option is None:
        raise TemplateContractError(
            "template evidence ids do not match the closed contract",
            "template_evidence_mismatch",
        )
    selected = [entries[evidence_id] for evidence_id in evidence_ref_ids]
    text, rationale = _render(template_id, evidence_ref_ids, entries, snapshot)
    return text, rationale, [entry.public_ref() for entry in selected]


def provider_evidence_catalog(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    referenced_ids = {
        evidence_id
        for option in build_template_catalog(snapshot)
        for evidence_id in option.evidence_ref_ids
    }
    return [
        entry.provider_value()
        for entry in build_evidence_catalog(snapshot)
        if entry.evidence_id in referenced_ids
    ]


def provider_template_catalog(snapshot: dict[str, Any]) -> list[dict[str, object]]:
    return [option.provider_value() for option in build_template_catalog(snapshot)]


def _entry(
    evidence_id: str,
    source: str,
    path: str,
    value: object,
) -> EvidenceEntry | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return EvidenceEntry(evidence_id, source, path, str(value)[:400])


def _display(value: str, *, limit: int = 160) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _money(value: str) -> str:
    return f"¥{int(value):,}/月"


def _dimension(snapshot: dict[str, Any], evidence_id: str) -> tuple[str, str]:
    match = _DIMENSION_ID_RE.fullmatch(evidence_id)
    if match is None:
        return "自定义比较项", ""
    path_id = match.group(1)
    offer = snapshot.get("offer_snapshot", {})
    dimensions = offer.get("dimensions", []) if isinstance(offer, dict) else []
    for dimension in dimensions if isinstance(dimensions, list) else []:
        if isinstance(dimension, dict) and dimension.get("path_id") == path_id:
            return _display(str(dimension.get("label") or "自定义比较项")), _display(
                str(dimension.get("value_text") or "")
            )
    return "自定义比较项", ""


def _render(
    template_id: str,
    evidence_ref_ids: list[str],
    entries: dict[str, EvidenceEntry],
    snapshot: dict[str, Any],
) -> tuple[str, str]:
    def value(evidence_id: str) -> str:
        return _display(entries[evidence_id].excerpt)

    if template_id == "goal_focus_request":
        return (
            f"本次沟通先聚焦一个目标：{value('brief.goal')}。",
            "先把目标说清楚，再讨论实现方式，避免谈薪变成没有落点的泛泛沟通。",
        )
    if template_id == "goal_interest_then_request":
        return (
            f"先表达你对 {value('offer.company_name')} · {value('offer.position_name')} 的加入意愿，"
            f"再提出本次诉求：{value('brief.goal')}。",
            "把加入意愿和具体诉求分开表达，便于对方准确回应你希望调整的部分。",
        )
    if template_id == "ask_current_compensation_structure":
        return (
            f"可以直接问：当前 {_money(value('offer.base_monthly'))} × "
            f"{value('offer.months_per_year')} 薪中，固定发放、绩效发放和其他条件分别是什么？",
            "先确认现有方案的组成，后续讨论调整时才不会混淆固定收入和浮动部分。",
        )
    if template_id == "ask_request_flexibility":
        return (
            f"可以直接问：关于“{value('brief.goal')}”，当前方案还有哪些可以讨论的空间？",
            "用开放式问题确认可协商范围，不预设对方政策或最终结果。",
        )
    if template_id == "ask_concern_details":
        return (
            f"针对你的顾虑“{value('brief.concerns')}”，可以问：这部分的具体规则、确认方式和时间点是什么？",
            "把顾虑转成可核实的问题，避免依靠猜测判断 Offer 风险。",
        )
    if template_id == "ask_offer_validity_after_request":
        return (
            "可以直接问：“如果这次薪酬讨论暂时无法达成一致，当前已经发出的 Offer 是否仍然有效，"
            "回复截止时间是否保持不变？”",
            f"这能直接回应你对“{value('brief.concerns')}”的顾虑，同时不预设对方会撤回或保留 Offer。",
        )
    if template_id == "ask_signing_bonus_terms":
        return (
            f"当前记录的签字费为 ¥{int(value('offer.signing_bonus')):,}，可以确认："
            "这一项是否为最终口径，以及是否附带发放或返还条件？",
            "把金额和适用条件一起确认，避免只记录一个数字。",
        )
    if template_id == "ask_equity_terms":
        return (
            f"当前股权记录为“{value('offer.equity')}”，可以确认授予数量、归属节奏和书面文件口径吗？",
            "现有记录只说明了股权概况，具体条件仍应由对方确认。",
        )
    if template_id == "ask_benefit_terms":
        return (
            f"当前福利记录为“{value('offer.perks')}”，可以确认适用范围、开始时间和书面口径吗？",
            "把福利描述转成可核实的条件，不把当前备注当作完整政策。",
        )
    if template_id == "ask_decision_deadline":
        return (
            f"可以确认：当前记录的决策截止时间 {value('offer.deadline')} 是否为最终时间，"
            "以及调整方案最晚何时能得到回复？",
            "同时确认决定期限和对方回复节奏，可以避免谈薪占用全部决策时间。",
        )
    if template_id == "ask_comparison_dimension":
        evidence_id = evidence_ref_ids[0]
        label, dimension_value = _dimension(snapshot, evidence_id)
        return (
            f"关于“{label}”（当前记录：{dimension_value}），可以确认具体口径以及是否会写入正式 Offer 吗？",
            "自定义比较项需要转成对方能够明确确认的条件。",
        )
    if template_id == "say_interest_and_request":
        return (
            f"“感谢贵司给出 Offer，我对 {value('offer.company_name')} 的 {value('offer.position_name')} 很感兴趣。"
            f"关于这次沟通，我希望争取：{value('brief.goal')}。想请问当前方案是否还有调整空间？”",
            "这段表达先确认意愿，再提出请求；你可以按自己的语气直接修改。",
        )
    if template_id == "say_current_offer_and_request":
        return (
            f"“我目前理解的方案是 {_money(value('offer.base_monthly'))} × "
            f"{value('offer.months_per_year')} 薪。我的期望是：{value('brief.goal')}。"
            "想请问固定月薪或整体方案是否还有调整空间？”",
            "引用已记录的当前方案，并把你的目标作为请求提出，不代替对方作出承诺。",
        )
    if template_id == "say_concern_and_request":
        return (
            f"“我的目标是：{value('brief.goal')}。同时我比较在意‘{value('brief.concerns')}’，"
            "想先确认相关规则，再一起讨论可行方案。”",
            "把诉求和顾虑放在同一段沟通中，便于对方分别回应。",
        )
    if template_id == "say_request_and_preserve_offer":
        return (
            f"“我希望就‘{value('brief.goal')}’再沟通一下。如果这次调整暂时无法实现，"
            "也请帮我确认当前 Offer 是否仍然有效，原回复截止时间是否保持不变。”",
            f"这段表达提出了你的诉求，也正面处理“{value('brief.concerns')}”这项顾虑，不替对方作出承诺。",
        )
    if template_id == "say_scenario_opening":
        return (
            f"如果通过{value('brief.scenario')}沟通，可以这样开场：“感谢你安排这次沟通。"
            f"我想围绕‘{value('brief.goal')}’确认一下当前方案是否还有调整空间。”",
            "提供可直接使用的开场，但不替你接受、拒绝或判断 Offer。",
        )
    if template_id == "check_current_compensation":
        return (
            f"沟通前核对当前记录：固定月薪 {_money(value('offer.base_monthly'))}、"
            f"{value('offer.months_per_year')} 薪；不确定的发放条件先列成问题。",
            "先锁定当前方案的事实基线，避免沟通时混淆已有条件和期望条件。",
        )
    if template_id == "check_goal_and_concern":
        return (
            f"沟通前写下最想争取的结果（{value('brief.goal')}）和可以接受的底线；"
            f"同时准备好对“{value('brief.concerns')}”的追问。",
            "目标、底线和顾虑分别准备，能让现场沟通更容易收束。",
        )
    if template_id == "check_decision_deadline":
        return (
            f"当前决策截止时间记录为 {value('offer.deadline')}；沟通前确认具体时间、时区和调整方案的回复期限。",
            "避免因时间口径不清影响后续决定。",
        )
    if template_id == "check_written_follow_up":
        return (
            f"如对方同意调整，请在决定前核对 {value('offer.company_name')} · "
            f"{value('offer.position_name')} 的更新版书面 Offer。",
            "口头沟通完成后仍需以对方提供的正式文件为准。",
        )
    if template_id == "check_comparison_dimension":
        evidence_id = evidence_ref_ids[0]
        label, dimension_value = _dimension(snapshot, evidence_id)
        return (
            f"沟通前核对“{label}”的当前记录（{dimension_value}），并标记仍需对方确认的部分。",
            "把自定义比较项作为核对清单，而不是把用户记录误当成对方承诺。",
        )
    raise TemplateContractError("template is not implemented", "invalid_field_type")
