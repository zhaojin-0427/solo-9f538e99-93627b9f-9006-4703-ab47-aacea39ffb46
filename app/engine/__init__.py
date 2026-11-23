# -*- coding: utf-8 -*-
"""问卷版号核查引擎（零第三方依赖，仅标准库）。"""
from .conditions import (
    ConditionError,
    EvalError,
    ast_to_text,
    evaluate,
    parse_condition,
    variables_in,
)
from .config import (
    ConfigError,
    Question,
    QuestionnairePair,
    VersionConfig,
    build_pair,
    build_version_config,
    version_to_jsonable,
)
from .compat import CompatResult, check_answer_value, check_compatibility
from .solver import (
    MAX_BATCH_SIZE,
    BatchError,
    BatchResult,
    CONFIRMED_NEW,
    CONFIRMED_OLD,
    PENDING,
    UNSOLVABLE,
    VERDICT_LABELS,
    ResponseData,
    build_batch,
    normalize_response,
    parse_batch,
    recompute_compat,
    solve,
)
from .store import (
    BatchStore,
    Snapshot,
    StoreError,
    batch_store,
    dump_snapshot_meta,
)

__all__ = [
    # conditions
    "ConditionError",
    "EvalError",
    "ast_to_text",
    "evaluate",
    "parse_condition",
    "variables_in",
    # config
    "ConfigError",
    "Question",
    "QuestionnairePair",
    "VersionConfig",
    "build_pair",
    "build_version_config",
    "version_to_jsonable",
    # compat
    "CompatResult",
    "check_answer_value",
    "check_compatibility",
    # solver
    "MAX_BATCH_SIZE",
    "BatchError",
    "BatchResult",
    "CONFIRMED_NEW",
    "CONFIRMED_OLD",
    "PENDING",
    "UNSOLVABLE",
    "VERDICT_LABELS",
    "ResponseData",
    "build_batch",
    "normalize_response",
    "parse_batch",
    "recompute_compat",
    "solve",
    # store
    "BatchStore",
    "Snapshot",
    "StoreError",
    "batch_store",
    "dump_snapshot_meta",
]
