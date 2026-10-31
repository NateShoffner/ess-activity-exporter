from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Source(BaseModel):
    facilitator: Optional[str] = None
    action: Optional[str] = None
    date: Optional[str] = None
    user_id: Optional[str] = None
    admin_id: Optional[str] = None


class MoveTarget(BaseModel):
    from_: Optional[str] = Field(None, alias="from")


class PlanConfiguration(BaseModel):
    move_allocators: List[MoveTarget] = []
    move_instances: List[MoveTarget] = []


class Transient(BaseModel):
    strategy: Dict[str, Any] = {}
    plan_configuration: PlanConfiguration = PlanConfiguration()


class Plan(BaseModel):
    model_config = ConfigDict(extra="allow")
    transient: Transient = Transient()

    def version(self, kind: str) -> Optional[str]:
        section = (self.model_extra or {}).get(kind)
        return section.get("version") if isinstance(section, dict) else None


class Step(BaseModel):
    step_id: Optional[str] = None
    status: Optional[str] = None
    info_log: List[Dict[str, Any]] = []


class PlanAttempt(BaseModel):
    plan_attempt_name: Optional[str] = None
    plan_attempt_id: Optional[str] = None
    attempt_start_time: Optional[str] = None
    attempt_end_time: Optional[str] = None
    healthy: Optional[bool] = None
    plan: Plan = Plan()
    source: Source = Source()
    warnings: List[Any] = []
    error: Optional[Dict[str, Any]] = None
    plan_attempt_log: List[Step] = []


class PlanInfo(BaseModel):
    current: Optional[PlanAttempt] = None
    history: List[PlanAttempt] = []


class ResourceInfo(BaseModel):
    id: Optional[str] = None
    plan_info: PlanInfo = PlanInfo()


class Resource(BaseModel):
    ref_id: Optional[str] = None
    info: ResourceInfo = ResourceInfo()


class Deployment(BaseModel):
    id: str
    name: Optional[str] = None
    resources: Dict[str, List[Resource]] = {}


class ActivityRow(BaseModel):
    deployment_name: str
    deployment_id: str
    resource_kind: str
    resource_ref_id: Optional[str]
    plan_attempt_name: Optional[str]
    plan_attempt_id: Optional[str]
    attempt_start_time: Optional[str]
    attempt_end_time: Optional[str]
    duration_seconds: Optional[float]
    healthy: Optional[bool]
    source_action: Optional[str]
    source_user: Optional[str]
    source_facilitator: Optional[str]
    source_date: Optional[str]
    initiated_by: str
    plan_strategy: Optional[str]
    description: str
    move_allocator_ids: List[str]
    move_instance_names: List[str]
    log_instance_names: List[str]
    version: Optional[str]
    error_failure_type: Optional[str]
    error_message: Optional[str]
    warnings_count: int
    steps_total: int
    steps_failed: int
    failed_step_ids: List[str]
