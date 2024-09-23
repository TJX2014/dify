from collections.abc import Callable, Sequence
from typing import Literal, cast

from core.file import File, file_manager
from core.helper import ssrf_proxy
from core.variables import ArrayFileSegment, ArrayNumberSegment, ArrayStringSegment
from core.workflow.entities.node_entities import NodeRunResult
from core.workflow.nodes.base_node import BaseNode
from enums.workflow_nodes import NodeType
from models.workflow import WorkflowNodeExecutionStatus

from .models import ListFilterNodeData


class ListFilterNode(BaseNode):
    _node_data_cls = ListFilterNodeData
    _node_type = NodeType.LIST_FILTER

    def _run(self):
        node_data = cast(ListFilterNodeData, self.node_data)
        inputs = {
            "filter_by": [filter_by.model_dump() for filter_by in node_data.filter_by],
            "order_by": node_data.order_by.model_dump(),
            "limit": node_data.limit.model_dump(),
        }
        process_data = {}
        outputs = {}

        variable = self.graph_runtime_state.variable_pool.get(node_data.variable)
        if variable is None:
            error_message = f"Variable not found for selector: {node_data.variable}"
            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.FAILED, error=error_message, inputs=inputs, outputs=outputs
            )
        if not isinstance(variable, ArrayFileSegment | ArrayNumberSegment | ArrayStringSegment):
            error_message = (
                f"Variable {node_data.variable} is not an ArrayFileSegment, ArrayNumberSegment " "or ArrayStringSegment"
            )
            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.FAILED, error=error_message, inputs=inputs, outputs=outputs
            )
        process_data["variable"] = variable.value

        # Filter
        for filter_by in node_data.filter_by:
            value = self.graph_runtime_state.variable_pool.convert_template(filter_by.value).text
            # process_data["filter_by_value"] = value
            if isinstance(variable, ArrayStringSegment):
                filter_func = _get_string_filter_func(condition=filter_by.comparison_operator, value=value)
                result = list(filter(filter_func, variable.value))
                variable = variable.model_copy(update={"value": result})
            elif isinstance(variable, ArrayNumberSegment):
                filter_func = _get_number_filter_func(condition=filter_by.comparison_operator, value=float(value))
                result = list(filter(filter_func, variable.value))
                variable = variable.model_copy(update={"value": result})
            elif isinstance(variable, ArrayFileSegment):
                filter_func = _get_file_filter_func(
                    key=filter_by.key,
                    condition=filter_by.comparison_operator,
                    value=value,
                )
                result = list(filter(filter_func, variable.value))
                variable = variable.model_copy(update={"value": result})

        # Order
        if node_data.order_by.enabled:
            if isinstance(variable, ArrayStringSegment):
                result = _order_string(order=node_data.order_by.value, array=variable.value)
                variable = variable.model_copy(update={"value": result})
            elif isinstance(variable, ArrayNumberSegment):
                result = _order_number(order=node_data.order_by.value, array=variable.value)
                variable = variable.model_copy(update={"value": result})
            elif isinstance(variable, ArrayFileSegment):
                result = _order_file(order=node_data.order_by.value, array=variable.value)
                variable = variable.model_copy(update={"value": result})

        # Slice
        if node_data.limit.enabled:
            result = variable.value[: node_data.limit.size]
            variable = variable.model_copy(update={"value": result})

        outputs["result"] = variable.value
        return NodeRunResult(
            status=WorkflowNodeExecutionStatus.SUCCEEDED,
            inputs=inputs,
            process_data=process_data,
            outputs=outputs,
        )


def _get_file_extract_number_func(*, key: str) -> Callable[[File], int]:
    match key:
        case "size":
            return _get_file_size
        case _:
            raise ValueError(f"Invalid key: {key}")


def _get_file_extract_string_func(*, key: str) -> Callable[[File], str]:
    match key:
        case "name":
            return lambda x: x.filename or ""
        case "type":
            return lambda x: x.type
        case "extension":
            return lambda x: x.extension or ""
        case "mimetype":
            return lambda x: x.mime_type or ""
        case "transfer_method":
            return lambda x: x.transfer_method
        case "urL":
            return lambda x: x.url or ""
        case _:
            raise ValueError(f"Invalid key: {key}")


def _get_file_size(file: File):
    if file.related_id:
        content = file_manager.download(upload_file_id=file.related_id, tenant_id=file.tenant_id)
        return len(content)
    elif file.url:
        response = ssrf_proxy.head(url=file.url)
        response.raise_for_status()
        return int(response.headers.get("Content-Length", 0))
    else:
        raise ValueError("Invalid file")


def _get_string_filter_func(*, condition: str, value: str) -> Callable[[str], bool]:
    match condition:
        case "contains":
            return _contains(value)
        case "startswith":
            return _startswith(value)
        case "endswith":
            return _endswith(value)
        case "is":
            return _is(value)
        case "in":
            return _in(value)
        case "empty":
            return lambda x: x == ""
        case "not contains":
            return lambda x: not _contains(value)(x)
        case "not is":
            return lambda x: not _is(value)(x)
        case "not in":
            return lambda x: not _in(value)(x)
        case "not empty":
            return lambda x: x != ""
        case _:
            raise ValueError(f"Invalid condition: {condition}")


def _get_number_filter_func(*, condition: str, value: int | float) -> Callable[[int | float], bool]:
    match condition:
        case "=":
            return _eq(value)
        case "!=":
            return _ne(value)
        case "<":
            return _lt(value)
        case "≤":
            return _le(value)
        case ">":
            return _gt(value)
        case "≥":
            return _ge(value)
        case _:
            raise ValueError(f"Invalid condition: {condition}")


def _get_file_filter_func(*, key: str, condition: str, value: str) -> Callable[[File], bool]:
    if key in {"name", "type", "extension", "mime_type", "transfer_method", "urL"}:
        extract_func = _get_file_extract_string_func(key=key)
        return lambda x: _get_string_filter_func(condition=condition, value=value)(extract_func(x))
    elif key == "size":
        extract_func = _get_file_extract_number_func(key=key)
        return lambda x: _get_number_filter_func(condition=condition, value=float(value))(extract_func(x))
    else:
        raise ValueError(f"Invalid key: {key}")


def _contains(value: str):
    return lambda x: value in x


def _startswith(value: str):
    return lambda x: x.startswith(value)


def _endswith(value: str):
    return lambda x: x.endswith(value)


def _is(value: str):
    return lambda x: x is value


def _in(value: str):
    return lambda x: x in value


def _eq(value: int | float):
    return lambda x: x == value


def _ne(value: int | float):
    return lambda x: x != value


def _lt(value: int | float):
    return lambda x: x < value


def _le(value: int | float):
    return lambda x: x <= value


def _gt(value: int | float):
    return lambda x: x > value


def _ge(value: int | float):
    return lambda x: x >= value


def _order_number(*, order: Literal["asc", "desc"], array: Sequence[int | float]):
    return sorted(array, key=lambda x: x, reverse=order == "desc")


def _order_string(*, order: Literal["asc", "desc"], array: Sequence[str]):
    return sorted(array, key=lambda x: x, reverse=order == "desc")


def _order_file(*, order: Literal["asc", "desc"], order_by: str = "", array: Sequence[File]):
    if order_by in {"name", "type", "extension", "mime_type", "transfer_method", "urL"}:
        extract_func = _get_file_extract_string_func(key=order_by)
        return sorted(array, key=lambda x: extract_func(x), reverse=order == "desc")
    elif order_by == "size":
        extract_func = _get_file_extract_number_func(key=order_by)
        return sorted(array, key=lambda x: extract_func(x), reverse=order == "desc")
    else:
        raise ValueError(f"Invalid order key: {order_by}")