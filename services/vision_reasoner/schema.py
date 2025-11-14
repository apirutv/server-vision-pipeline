# services/vision_reasoner/schema.py
from __future__ import annotations

from typing import List, Literal, Optional, Dict, Any

from pydantic import BaseModel, Field


# Primitive enums / literals
CommandType = Literal["direct", "semi_direct", "indirect"]
ScopeType = Literal["camera", "zone", "whole_house"]
TimeWindowType = Literal["relative", "absolute", "pattern_default"]
AggregationMode = Literal["raw_events", "timeline", "pattern_summary"]
OutputFormat = Literal["clips", "frames", "text_summary", "clips_plus_summary"]


class RelativeTime(BaseModel):
    """
    Optional human-oriented description of a relative window.
    Example: keyword="yesterday", offset_days=1
    """

    keyword: str = Field(
        ...,
        description=(
            "Optional human label like 'yesterday', 'last_night_after_22', "
            "'two_days_ago', 'last_14_days'. Used for explanation."
        ),
    )
    offset_days: Optional[int] = Field(
        None,
        description="If applicable, number of days in the past.",
    )


class TimeWindow(BaseModel):
    """
    Time resolution:
      - The LLM decides from_iso / to_iso (ISO-8601, with timezone).
      - Backend converts to from_ts / to_ts as epoch seconds.

    Types:
      - relative: expressions like 'yesterday', 'last night', 'last 7 days'
      - absolute: user gave explicit dates/times
      - pattern_default: 'usual dinner time', 'habit' / pattern queries
    """

    type: TimeWindowType

    # LLM outputs concrete ISO start/end, e.g. "2025-11-12T00:00:00+07:00"
    from_iso: Optional[str] = Field(
        None,
        description="ISO-8601 start time (with timezone) decided by the LLM.",
    )
    to_iso: Optional[str] = Field(
        None,
        description="ISO-8601 end time (with timezone) decided by the LLM.",
    )

    # Backend populates from these ISO values
    from_ts: Optional[float] = Field(
        None,
        description="Epoch seconds for start time (backend fills from from_iso).",
    )
    to_ts: Optional[float] = Field(
        None,
        description="Epoch seconds for end time (backend fills from to_iso).",
    )

    # Optional relative descriptor, mainly for debugging / transparency
    relative: Optional[RelativeTime] = None


class TargetScope(BaseModel):
    """
    What part of the home / which cameras the question applies to.
    """

    cameras: List[str] = Field(
        default_factory=list,
        description="Concrete camera IDs e.g. ['street', 'gate_left']",
    )
    zones: List[str] = Field(
        default_factory=list,
        description="Logical zones e.g. ['front_gate', 'dining_area']",
    )
    scope_type: ScopeType = Field(
        ...,
        description="'camera' | 'zone' | 'whole_house'",
    )


class EventFilter(BaseModel):
    """
    Semantic filter for what kinds of events we care about.
    """

    subjects: List[str] = Field(
        default_factory=list,
        description="Semantic subjects e.g. ['person', 'vehicle', 'pet']",
    )
    activities: List[str] = Field(
        default_factory=list,
        description="Activities e.g. ['entering', 'leaving', 'sitting']",
    )
    confidence_threshold: float = Field(
        0.4,
        description="Minimum confidence for events to be considered.",
    )


class Aggregation(BaseModel):
    """
    How we want results aggregated.
    """

    mode: AggregationMode = Field(
        ...,
        description="'raw_events' | 'timeline' | 'pattern_summary'",
    )
    group_by: List[str] = Field(
        default_factory=list,
        description="E.g. ['hour_of_day'] for pattern_summary.",
    )
    top_k: int = Field(
        100,
        description="Maximum number of events/items to retrieve from RAG.",
    )


class VisionQueryPlan(BaseModel):
    """
    Full plan produced by the LLM for a camera/RAG query.
    This is what we validate in reasoner.py and execute in executor.py.
    """

    command_type: CommandType = Field(
        ...,
        description="'direct' | 'semi_direct' | 'indirect'",
    )
    target_scope: TargetScope
    time_window: TimeWindow
    event_filter: EventFilter
    aggregation: Aggregation
    output_format: OutputFormat = Field(
        ...,
        description="'clips' | 'frames' | 'text_summary' | 'clips_plus_summary'",
    )

    needs_clarification: bool = Field(
        False,
        description=(
            "If true, backend should not call RAG yet. "
            "Instead, ask the user clarification_question."
        ),
    )
    clarification_question: Optional[str] = Field(
        None,
        description="Single short clarification question for the user.",
    )
    reason_brief: str = Field(
        "",
        description="Short explanation of how the request was interpreted.",
    )
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="LLM self-estimated confidence in this plan.",
    )

    extra: Dict[str, Any] = Field(
        default_factory=dict,
        description="Reserved for future extensions; ignored by core engine.",
    )
