from __future__ import annotations

from uuid import uuid4

from agent_quant_system.models import ExecutionReceipt, OrderProposal, RiskDecision


class PaperExecutionGateway:
    """Deterministic fill-at-reference-price gateway for local smoke tests."""

    def __init__(self) -> None:
        self.receipts: list[ExecutionReceipt] = []
        self._submitted: dict[str, tuple[OrderProposal, ExecutionReceipt]] = {}

    def submit(self, decision: RiskDecision) -> ExecutionReceipt:
        if not decision.approved or decision.order is None:
            raise ValueError("execution gateway accepts approved decisions only")

        order = decision.order
        previous = self._submitted.get(order.client_order_id)
        if previous:
            if previous[0] != order:
                raise ValueError("client order ID reused with different order contents")
            return previous[1]
        receipt = ExecutionReceipt(
            client_order_id=order.client_order_id,
            broker_order_id=f"paper-{uuid4().hex}",
            status="FILLED",
            ticker=order.ticker,
            action=order.action,
            filled_quantity=order.quantity,
            average_price=order.reference_price,
        )
        self.receipts.append(receipt)
        self._submitted[order.client_order_id] = (order, receipt)
        return receipt
