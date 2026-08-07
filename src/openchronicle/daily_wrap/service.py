"""Grounded Daily Wrap application service."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from ..config import Config
from ..memory_candidates import store as candidate_store
from ..prompts import load as load_prompt
from ..provenance.models import EvidenceRef
from ..services.context import ContextRecord, ContextService, DayContext
from ..writer import llm as llm_mod
from . import store

WORKFLOW_VERSION = 1
_CATEGORIES = ("completed", "progressed", "open", "blocked", "needs_review")
_EXPLICIT_SIGNALS: dict[str, re.Pattern[str]] = {
    "completed": re.compile(
        r"(?:^|[\n.!?;:]\s*|\]\s*)(?:successfully\s+)?"
        r"(?:done|completed|finished|merged|shipped|sent|closed|resolved|deployed)\b|"
        r"\b(?:is|are|was|were|has been|have been)\s+(?:successfully\s+)?"
        r"(?:done|completed|finished|merged|shipped|sent|closed|resolved|deployed)\b|"
        r"(?:^|[\n。！？；：]\s*|\]\s*)(?:已完成|完成了|已合并|已提交|已发送|已解决|已上线)",
        re.IGNORECASE,
    ),
    "open": re.compile(
        r"(?:^|[\n.!?;:]\s*|\]\s*)"
        r"(?:todo|next|remaining|pending|follow[- ]?up|unfinished)\b|"
        r"\b(?:is|are|remains?|stays?)\s+(?:open|pending|unfinished)\b|"
        r"(?:^|[\n。！？；：]\s*|\]\s*)(?:尚未完成|下一步|待办|待处理|仍需|需要继续)",
        re.IGNORECASE,
    ),
    "blocked": re.compile(
        r"(?:^|[\n.!?;:]\s*|\]\s*)"
        r"(?:blocked|waiting\s+for|error|failure|failed|unavailable|stuck)\b|"
        r"\b(?:is|are|was|were|remains?)\s+(?:currently\s+)?blocked\b|"
        r"\b(?:build|tests?|job|deployment|request|command|run)\s+(?:has\s+)?failed\b|"
        r"(?:^|[\n。！？；：]\s*|\]\s*)(?:卡住|阻塞|等待|错误|失败|不可用)",
        re.IGNORECASE,
    ),
}
_NEGATED_COMPLETION = re.compile(
    r"\b(?:no|not|never|none|nothing|neither|isn['’]t|wasn['’]t|hasn['’]t|"
    r"haven['’]t|didn['’]t|unable|failed|incomplete)\b.{0,80}"
    r"\b(?:done|completed|finished|merged|shipped|sent|closed|resolved|deployed)\b|"
    r"\b(?:done|completed|finished|merged|shipped|sent|closed|resolved|deployed)\b"
    r".{0,40}\b(?:not|never|only partially|no longer)\b|"
    r"\b(?:remains?|yet|still|needs?|required|planned|expected)\b.{0,50}"
    r"\b(?:to\s+be\s+)?(?:done|completed|finished|merged|shipped|sent|closed|resolved|deployed)\b|"
    r"\b(?:will|would|should|could|may|might|must)\b.{0,50}"
    r"\b(?:be\s+)?(?:done|completed|finished|merged|shipped|sent|closed|resolved|deployed)\b|"
    r"\banything\s+but\b.{0,40}"
    r"\b(?:done|completed|finished|merged|shipped|sent|closed|resolved|deployed)\b|"
    r"\b(?:done|completed|finished|merged|shipped|sent|closed|resolved|deployed)\b"
    r".{0,40}\b(?:but|however|actually|in\s+fact)\b.{0,20}"
    r"\b(?:failed|blocked|cancell?ed|incomplete|unfinished)\b|"
    r"未完成|尚未完成|没有完成|还没完成|不算完成|并非完成|不能算完成|"
    r"未合并|未提交|未发送|未解决|未上线|计划.{0,20}(?:完成|合并|提交|发送|上线)|"
    r"(?:明天|稍后|之后).{0,20}(?:完成|合并|提交|发送|上线)",
    re.IGNORECASE,
)
_NEGATED_OPEN = re.compile(
    r"\b(?:no|none|nothing|zero)\b.{0,80}"
    r"\b(?:todo|remaining|pending|follow[- ]?up|unfinished)\b|"
    r"\b(?:todo|remaining|pending|follow[- ]?up|unfinished)\b.{0,60}"
    r"\b(?:no|no longer|not|not needed|none|false|zero|done|completed|cancelled|canceled|removed|closed)\b|"
    r"\b(?:cancelled|canceled|removed|closed)\b.{0,60}"
    r"\b(?:todo|remaining|pending|follow[- ]?up|unfinished)\b|"
    r"没有待办|已无待办|无需跟进|不再需要|没有剩余|已完成|已取消|已移除|已关闭",
    re.IGNORECASE,
)
_RESOLVED_BLOCKER = re.compile(
    r"\b(?:fixed|resolved|recovered|cleared|unblocked)\b.{0,80}"
    r"\b(?:error|failure|block(?:ed|er|ing)?)\b|"
    r"\b(?:error|failure|block(?:ed|er|ing)?)\b.{0,80}"
    r"\b(?:fixed|resolved|recovered|cleared|unblocked)\b|"
    r"\b(?:not|no longer|never)\b.{0,60}\b(?:blocked|waiting|error|failure)\b|"
    r"\b(?:error|failure)\b.{0,60}\b(?:no|none|false|not present|no longer|gone|stopped|ended|doesn['’]t occur)\b|"
    r"\b(?:stopped|finished|ended)\b.{0,40}\bwaiting\b|"
    r"(?:错误|失败|阻塞).{0,30}(?:已修复|已解决|恢复|解除)|"
    r"(?:已修复|已解决|恢复|解除).{0,30}(?:错误|失败|阻塞)",
    re.IGNORECASE,
)
_UNCERTAIN_STRONG_CLAIM = re.compile(
    r"\b(?:if|unless|provided|assuming|once|when)\b|"
    r"(?:^|\s)[-*+]?\s*\[\s\]|\byes\s+or\s+no\b|"
    r"[:：=\-–—]\s*(?:no|none|false|not|never|off|0|n\s*/\s*a)\b|"
    r"(?:吗|么|嘛|呢|没|吧)(?:[啊呀呢吧嘛]?[。.!…]*)$|"
    r"(?:已?完成(?:了)?)(?:没有|没|否)[啊呀呢吧嘛]?[。.!…]*$|"
    r"如果|若(?:是|果)?|一旦|取决于|"
    r"[:：=\-–—]\s*(?:否|不是|没有|无|关闭|0)(?:[。.!…]|$)",
    re.IGNORECASE,
)
_PROMPT_CONTROL_TEXT = re.compile(
    r"</?[a-z][^>\r\n]{0,120}>|"
    r"(?:https?://|www\.|mailto:)|"
    r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"\b(?:system|assistant|developer|tool|agent|model)\b\s*(?:message)?\s*[,;:：\-–—]|"
    r"\b(?:ignore|disregard|override|forget|obey)\b|"
    r"\b(?:instruction|direction|command|prompt)s?\b|"
    r"\b(?:password|passcode|credential|secret|api\s*key|private\s*key|access\s*token)\b|"
    r"\b(?:you\s+(?:must|should)|please)\b.{0,100}"
    r"\b(?:follow|execute|run|call|invoke|visit|open(?![-‐‑‒–—]sourced\b)|"
    r"click|enter|type|paste|"
    r"forward|reveal|expose|upload|exfiltrate|send|delete|reply|transfer)\b|"
    r"(?:^|[\n.!?;:]\s*|\]\s*)(?:follow|execute|run|call|invoke|visit|"
    r"open(?![-‐‑‒–—]sourced\b)|"
    r"click|enter|type|paste|forward|reveal|expose|upload|exfiltrate|send|"
    r"delete|reply|transfer)\b|"
    r"\b(?:follow|execute|run|call|invoke|visit|open(?![-‐‑‒–—]sourced\b)|"
    r"click|enter|type|paste|"
    r"forward|reveal|expose|upload|exfiltrate|send|delete|reply|transfer)\b.{0,80}"
    r"\b(?:instruction|direction|command|tool|email|file|key|token)\b|"
    r"(?:^|\s)(?:sudo\s+)?(?:rm|curl|wget|bash|sh|powershell)\s+[-/]|"
    r"(?:系统|助手|开发者|工具|代理|模型)\s*[,，;；:：]|"
    r"(?:密码|口令|凭据|秘密|密钥|令牌)|"
    r"(?:^|[\n。！？；：]\s*|\]\s*)(?:忽略|无视|覆盖|服从|遵循|执行|运行|"
    r"调用|访问|打开|点击|输入|粘贴|转发|回复|上传|发送|删除|泄露|转账)",
    re.IGNORECASE,
)
_PROMPT_COMMAND_TEXT = re.compile(
    r"(?:^|[\n,.!?;:，。！？；：])\s*"
    r"(?:(?:[-*+#>•‣◦▪●–—]|(?:\d{1,3}|[a-z])(?:[.)])?)\s+|"
    r"[\"'`“”‘’«»‹›([{]\s*){0,4}"
    r"(?:(?:system|assistant|developer|tool|agent|model)\s*"
    r"(?:message\s*)?(?:[,;:：\-–—]\s*)?)?"
    r"(?:(?:now|then|next|kindly|only|always|never|just|immediately|ever)"
    r"\s*,?\s*)*"
    r"(?:follow|execute|run|call|invoke|visit|open(?![-‐‑‒–—]sourced\b)|"
    r"click|enter|type|paste|"
    r"forward|reveal|expose|upload|exfiltrate|send|delete|reply|transfer|"
    r"output|return|respond|print|write|say|repeat|emit|display|answer|provide|"
    r"publish|post|share|disclose|export|copy|move|rename|install|download|"
    r"launch|create|modify|change|approve|confirm|authorize|purchase|pay|"
    r"act|pretend|set|make|use|ensure|replace|do)\b",
    re.IGNORECASE,
)
_PROMPT_REQUEST_TEXT = re.compile(
    r"\b(?:can|could|would|will|should|may)\s+you\b|"
    r"\b(?:(?:can|could|would)\s+it\s+be|is\s+it)\s+possible\s+to\b|"
    r"\byour\s+(?:task|instruction|job)\s+is\s+to\b|"
    r"\b(?:i|we)\s+(?:want|need|ask|require)\s+you\s+to\b",
    re.IGNORECASE,
)
_PROMPT_META_TEXT = re.compile(
    r"\b(?:from\s+now\s+on|act\s+as|pretend\s+to\s+be|role[- ]?play\s+as)\b|"
    r"\b(?:your|the)\s+(?:answer|response|output|reply)\s+"
    r"(?:must|should|shall|needs?\s+to|has\s+to)\b|"
    r"\b(?:answer|response|output|reply)\b.{0,30}"
    r"\b(?:exactly|only|must|should|shall)\b|"
    r"\b(?:the\s+)?(?:only\s+)?(?:valid|required)\s+"
    r"(?:answer|response|output|reply)\s*(?:is|[:：=])|"
    r"\brequired\s+(?:answer|response|output|reply)\s*[:：=]|"
    r"\b(?:must|should|shall|needs?\s+to|has\s+to)\s+be\s+"
    r"(?:your|the)\s+(?:only\s+)?(?:answer|response|output|reply)\b",
    re.IGNORECASE,
)
_PROMPT_CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "х": "x",
        "у": "y",
        "і": "i",
        "ј": "j",
        "к": "k",
        "м": "m",
        "т": "t",
        "в": "b",
        "н": "h",
        "ӏ": "l",
        "α": "a",
        "ε": "e",
        "ο": "o",
        "ρ": "p",
        "χ": "x",
        "υ": "y",
        "ι": "i",
        "κ": "k",
        "μ": "m",
        "τ": "t",
    }
)
_SEMANTIC_SYMBOLS = str.maketrans({"≠": "!="})
_MAX_REMOTE_PAYLOAD_BYTES = 200_000


class DailyWrapValidationError(RuntimeError):
    """Provider output did not satisfy the evidence contract."""


class DailyWrapInputChanged(RuntimeError):
    """The day changed while a provider call was in flight."""


class DailyWrapService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        *,
        llm_caller: Callable[..., Any] | None = None,
    ):
        self.conn = conn
        self.cfg = cfg
        self.llm_caller = llm_caller or llm_mod.call_llm
        self.context_service = ContextService(conn, cfg)

    def run(
        self,
        local_date: date,
        timezone: str,
        *,
        scope: str = "default",
        lease_token: str | None = None,
        cancelled: Callable[[], bool] | None = None,
        claim_guard: contextlib.AbstractContextManager[object] | None = None,
    ) -> store.DailyWrapRow:
        _raise_if_cancelled(cancelled)
        context = self.context_service.for_day(local_date, timezone)
        input_digest = context.input_digest(workflow_version=WORKFLOW_VERSION)
        lease_token = lease_token or uuid.uuid4().hex
        lease_seconds = max(
            self.cfg.daily_wrap.lease_seconds,
            math.ceil(llm_mod.call_budget_seconds(self.cfg, "daily_wrap")),
        )
        guard = claim_guard or contextlib.nullcontext()
        with guard:
            _raise_if_cancelled(cancelled)
            claim = store.claim(
                self.conn,
                local_date=local_date.isoformat(),
                timezone=timezone,
                scope=scope,
                window_start_utc=context.window_start_utc.isoformat(),
                window_end_utc=context.window_end_utc.isoformat(),
                workflow_version=WORKFLOW_VERSION,
                coverage_status=context.coverage_status,
                input_digest=input_digest,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
            )
        if not claim.claimed:
            return claim.row

        try:
            _raise_if_cancelled(cancelled)
            output, cited_sources, coverage_status = self._generate(context)
            _raise_if_cancelled(cancelled)
            latest_context = self.context_service.for_day(local_date, timezone)
            if latest_context.input_digest(workflow_version=WORKFLOW_VERSION) != input_digest:
                raise DailyWrapInputChanged(
                    "daily wrap input changed during generation; stale output discarded"
                )
            _raise_if_cancelled(cancelled)
            return store.complete(
                self.conn,
                wrap_id=claim.row.id,
                lease_token=lease_token,
                input_digest=input_digest,
                coverage_status=coverage_status,
                output=output,
                sources=cited_sources,
            )
        except BaseException as exc:
            store.fail(
                self.conn,
                wrap_id=claim.row.id,
                lease_token=lease_token,
                input_digest=input_digest,
                error=f"{type(exc).__name__}: generation failed",
            )
            raise

    def get(
        self, local_date: date, timezone: str, *, scope: str = "default"
    ) -> store.DailyWrapRow | None:
        row = store.get(
            self.conn,
            local_date=local_date.isoformat(),
            timezone=timezone,
            scope=scope,
        )
        if row and candidate_store.is_tombstoned(
            self.conn, kind="daily_wrap", artifact_id=row.id
        ):
            return None
        return row

    def list(self, *, limit: int = 30) -> list[store.DailyWrapRow]:
        return [
            row
            for row in store.list_wraps(self.conn, limit=limit)
            if not candidate_store.is_tombstoned(
                self.conn, kind="daily_wrap", artifact_id=row.id
            )
        ]

    def _generate(
        self, context: DayContext
    ) -> tuple[dict[str, Any], list[EvidenceRef], str]:
        if not context.records:
            output = self._empty_output(context)
            return output, [], context.coverage_status
        payload = {
            "local_date": context.local_date.isoformat(),
            "timezone": context.timezone,
            "window_start_utc": context.window_start_utc.isoformat(),
            "window_end_utc": context.window_end_utc.isoformat(),
            "coverage_status": context.coverage_status,
            "coverage_gaps": list(context.coverage_gaps),
            "records": [record.prompt_dict() for record in context.records],
        }
        payload_text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(payload_text.encode("utf-8")) > _MAX_REMOTE_PAYLOAD_BYTES:
            raise DailyWrapValidationError("bounded daily wrap payload exceeded limit")
        response = self.llm_caller(
            self.cfg,
            "daily_wrap",
            messages=[
                {"role": "system", "content": load_prompt("daily_wrap.md")},
                {
                    "role": "user",
                    "content": payload_text,
                },
            ],
            json_mode=True,
        )
        raw_text = llm_mod.extract_text(response).strip()
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise DailyWrapValidationError("daily wrap response was not valid JSON") from exc
        if not isinstance(raw, dict):
            raise DailyWrapValidationError("daily wrap response must be an object")
        return self._validate_output(context, raw)

    def _validate_output(
        self, context: DayContext, raw: dict[str, Any]
    ) -> tuple[dict[str, Any], list[EvidenceRef], str]:
        records_by_token = {record.evidence.key: record for record in context.records}
        result_items: dict[str, list[dict[str, Any]]] = {
            category: [] for category in _CATEGORIES
        }
        cited: dict[tuple[str, str, str], EvidenceRef] = {}
        rejected = 0
        for category in _CATEGORIES:
            values = raw.get(category, [])
            if not isinstance(values, list):
                raise DailyWrapValidationError(f"{category} must be an array")
            seen_item_ids: set[str] = set()
            for value in values[:100]:
                parsed = self._validate_item(
                    context=context,
                    category=category,
                    value=value,
                    records_by_token=records_by_token,
                )
                if parsed is None:
                    rejected += 1
                    continue
                item, item_records = parsed
                if item["id"] in seen_item_ids:
                    continue
                seen_item_ids.add(str(item["id"]))
                result_items[category].append(item)
                for record in item_records:
                    ref = record.evidence
                    cited[(ref.kind, ref.path, ref.id)] = ref

        gaps = list(context.coverage_gaps)
        if rejected:
            gaps.append(f"unsupported_model_items_rejected:{rejected}")
        coverage_status = "ready" if not gaps else "partial"
        grounded_count = sum(len(items) for items in result_items.values())
        output: dict[str, Any] = {
            "schema_version": 1,
            "local_date": context.local_date.isoformat(),
            "timezone": context.timezone,
            "status": coverage_status,
            "summary": (
                f"{grounded_count} grounded item(s) from "
                f"{len(cited)} cited source(s)."
            ),
            **result_items,
            "coverage_gaps": sorted(set(gaps)),
            "generated_at": datetime.now().astimezone().isoformat(),
        }
        return output, list(cited.values()), coverage_status

    def _validate_item(
        self,
        *,
        context: DayContext,
        category: str,
        value: Any,
        records_by_token: dict[str, ContextRecord],
    ) -> tuple[dict[str, Any], list[ContextRecord]] | None:
        if not isinstance(value, dict):
            return None
        text = str(value.get("text") or "").strip()
        supporting_text = str(value.get("supporting_text") or "").strip()
        raw_tokens = value.get("evidence")
        if (
            not text
            or text != supporting_text
            or len(text) > 500
            or _looks_like_prompt_control(supporting_text)
            or not isinstance(raw_tokens, list)
            or not raw_tokens
        ):
            return None
        tokens = [str(token) for token in raw_tokens]
        if len(tokens) > 20 or len(set(tokens)) != len(tokens):
            return None
        records: list[ContextRecord] = []
        for token in tokens:
            record = records_by_token.get(token)
            if record is None:
                return None
            records.append(record)
        if not any(supporting_text in record.text for record in records):
            return None
        if not _has_explicit_signal(category, supporting_text):
            return None
        refs = [record.evidence for record in records]
        item_id = _item_id(context.local_date, category, refs)
        return (
            {
                "id": item_id,
                "kind": category,
                "text": supporting_text,
                "supporting_text": supporting_text,
                "untrusted_activity_quote": True,
                "evidence": [ref.to_dict() for ref in refs],
            },
            records,
        )

    def _empty_output(self, context: DayContext) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "local_date": context.local_date.isoformat(),
            "timezone": context.timezone,
            "status": context.coverage_status,
            "summary": "No grounded activity items were available.",
            **{category: [] for category in _CATEGORIES},
            "coverage_gaps": list(context.coverage_gaps),
            "generated_at": datetime.now().astimezone().isoformat(),
        }


def _item_id(local_date: date, category: str, refs: list[EvidenceRef]) -> str:
    material = "\0".join(
        [
            "daily-wrap-item-v1",
            local_date.isoformat(),
            category,
            *sorted(f"{ref.kind}\0{ref.path}\0{ref.id}" for ref in refs),
        ]
    )
    return "wrap-item-" + hashlib.sha256(material.encode()).hexdigest()[:20]


def _has_explicit_signal(category: str, supporting_text: str) -> bool:
    supporting_text = _normalize_untrusted_text(supporting_text)
    signal = _EXPLICIT_SIGNALS.get(category)
    if signal is None:
        return True
    if _contains_question_marker(supporting_text):
        return False
    if _status_field_is_uncertain(category, supporting_text):
        return False
    if _UNCERTAIN_STRONG_CLAIM.search(supporting_text):
        return False
    if not signal.search(supporting_text):
        return False
    if category == "completed" and _NEGATED_COMPLETION.search(supporting_text):
        return False
    if category == "open" and _NEGATED_OPEN.search(supporting_text):
        return False
    return not (
        category == "blocked" and _RESOLVED_BLOCKER.search(supporting_text)
    )


class DailyWrapCancelled(RuntimeError):
    """A supervised background run was abandoned during daemon shutdown."""


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise DailyWrapCancelled("daily wrap cancelled during daemon shutdown")


def _looks_like_prompt_control(text: str) -> bool:
    normalized = _normalize_untrusted_text(text).translate(_PROMPT_CONFUSABLES)
    return bool(
        _PROMPT_CONTROL_TEXT.search(normalized)
        or _PROMPT_COMMAND_TEXT.search(normalized)
        or _PROMPT_REQUEST_TEXT.search(normalized)
        or _PROMPT_META_TEXT.search(normalized)
    )


def _normalize_untrusted_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.translate(_SEMANTIC_SYMBOLS))
    return "".join(
        char
        for char in normalized
        if unicodedata.category(char) not in {"Cf", "Mn", "Me"}
    ).casefold()


def _contains_question_marker(text: str) -> bool:
    for char in text:
        name = unicodedata.name(char, "")
        if char in {"?", "？"} or "QUESTION MARK" in name or "INTERROBANG" in name:
            return True
    return False


_STATUS_FIELD_PREFIX = (
    r"^\s*(?:(?:[-*+]\s+)|(?:\d{1,3}[.)]\s+)|"
    r"(?:\[[^\]\r\n]{1,32}\]\s+))*"
    r"(?:(?:status|state)\s*[:：]\s*)?"
)
_STATUS_FIELD_SEPARATOR = (
    r"(?P<separator>!=|≠|[:：,=]|\bis\b|\bare\b|\bwas\b|\bwere\b|"
    r"\bhas\s+been\b|\bhave\s+been\b|\bremains?\b)"
)
_STATUS_FIELD_RULES: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    "completed": (
        re.compile(
            _STATUS_FIELD_PREFIX
            + r"(?:completed|done|finished|resolved|merged|shipped|sent|closed|deployed)\s*"
            + _STATUS_FIELD_SEPARATOR
            + r"\s*(?P<value>.*?)\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:no|none|false|not|never|off|0|n\s*/\s*a|pending|"
            r"unchecked|incomplete|unfinished|open|todo|remaining|disabled|"
            r"planned|expected|unknown|failed|blocked|cancell?ed|"
            r"[✗✘❌⛔🚫])$",
            re.IGNORECASE,
        ),
    ),
    "open": (
        re.compile(
            _STATUS_FIELD_PREFIX
            + r"(?:todo|next|remaining|pending|follow[- ]?up|unfinished|open)\s*"
            + _STATUS_FIELD_SEPARATOR
            + r"\s*(?P<value>.*?)\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:no|none|false|not|never|off|0|n\s*/\s*a|done|"
            r"complete(?:d)?|finished|closed|cancell?ed|removed|disabled|"
            r"clear(?:ed)?|resolved|unknown|[✗✘❌⛔🚫])$",
            re.IGNORECASE,
        ),
    ),
    "blocked": (
        re.compile(
            _STATUS_FIELD_PREFIX
            + r"(?:blocked|waiting(?:\s+for)?|error|failure|failed|unavailable|stuck)\s*"
            + _STATUS_FIELD_SEPARATOR
            + r"\s*(?P<value>.*?)\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:no|none|false|not|never|off|0|n\s*/\s*a|clear(?:ed)?|"
            r"resolved|fixed|recovered|unblocked|ok(?:ay)?|disabled|gone|"
            r"ended|unknown|cancell?ed|[✓✔✅])$",
            re.IGNORECASE,
        ),
    ),
}


def _status_field_is_uncertain(category: str, text: str) -> bool:
    rule = _STATUS_FIELD_RULES.get(category)
    if rule is None:
        return False
    pattern, conflicting_value = rule
    match = pattern.fullmatch(text)
    if match is None:
        return False
    separator = match.group("separator")
    value = match.group("value").strip().rstrip(".!。")
    return separator in {"!=", "≠"} or not value or bool(conflicting_value.fullmatch(value))
