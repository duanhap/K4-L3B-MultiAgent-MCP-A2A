# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | `case: dict`, `gateway`, `trace` | Phân tích investigation_scope, giao việc, handoff, tổng hợp kết quả | Không gọi MCP trực tiếp | Output dict (l3b-output-v2) |
| Entity/customer | `candidate_order_ids`, `customer_unique_id_hint`, `claimed_order_id` | Resolve candidate IDs, lọc reject, tra cứu lịch sử khách hàng | `get_order`, `get_customer_history` | `EntityResult` (handoff cho Coordinator & Specialists) |
| Order/product | `resolved_order_ids`, `include_product_context` | Lấy chi tiết đơn hàng, danh sách item, seller_id, thông tin sản phẩm | `get_order`, `get_order_items`, `get_product_context` | `OrderResult` (handoff cho Shipment, Policy) |
| Shipment | `resolved_order_ids`, order timestamps | Phân tích timeline giao hàng, đối soát SLA của seller, chẩn đoán trễ | `get_shipment_summary`, `get_sellers` | `ShipmentResult` (handoff cho Policy) |
| Payment/refund | `resolved_order_ids` | Đối soát số tiền thanh toán, lịch sử hoàn tiền | `get_order_payments`, `get_refund_timeline`, `get_payment_timeline` | `PaymentResult` (handoff cho Policy) |
| Policy | Kết quả từ các Specialist agents + `policy_version` | Áp dụng quy tắc tính trách nhiệm, nguyên nhân, kiến nghị refund | `get_policy` (tùy chọn) | `PolicyResult` (handoff cho Conflict Resolver & Verifier) |
| Conflict resolver | Evidence từ các Specialist agents | Phát hiện mâu thuẫn giữa các nguồn (shipment vs order, payment) | Không gọi MCP trực tiếp | `ConflictResult` (handoff cho Verifier) |
| Verifier | Toàn bộ kết quả agents | Kiểm tra invariants, hiệu chỉnh confidence, kiểm tra evidence | Không gọi MCP trực tiếp | Final confidence & verified evidence_refs |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Mô tả cách xếp hạng/reject candidate, confidence threshold, message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Không trace nội dung suy luận riêng.
- Loop qua từng candidate trong `candidate_order_ids`.
- Gọi `get_order` để xác thực sự tồn tại và thông tin đơn hàng.
- Nếu không tìm thấy hoặc lỗi: ghi nhận vào `rejected_candidates`.
- Đối chiếu với `claimed_order_id` để tăng độ tin cậy (`confidence = 1.0` nếu match duy nhất).
- Lấy `customer_unique_id` và gọi `get_customer_history` nếu yêu cầu context.
- Trao đổi dữ liệu qua các Typed Dataclasses nội bộ (`EntityResult`, `OrderResult`, v.v.).

## 4. Evidence và conflict lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, chọn source theo policy, biểu diễn unresolved conflict, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.
- Mỗi lượt gọi MCP qua `EvidenceGateway` trả về `evidence_ref` duy nhất (prefix `ev_`).
- Emit sự kiện `tool_result_consumed` với `evidence_refs` tương ứng.
- Đụng độ thông tin giữa các nguồn:
  - Timestamp giao hàng mâu thuẫn: `prefer_shipment_record`.
  - Số tiền thanh toán mâu thuẫn: `prefer_payment_record`.
  - Trạng thái đơn hàng mâu thuẫn: `prefer_order_record`.
  - Không thể xác định: `unresolvable_conflict`.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / error | 2 retries (0.5s, 1s backoff) | Đánh dấu `insufficient_evidence` | `decision_code="mcp_retry_exhausted"` |
| Entity not found | 0 retries | Đặt `status="not_found"`, confidence 0.0 | `decision_code="entity_not_found"` |
| Entity ambiguous | 0 retries | Đặt `status="ambiguous"`, giảm confidence | `decision_code="entity_ambiguous"` |
| Source conflict | 0 retries | Ghi nhận vào `data_conflicts` | Không cần dừng luồng |
| Specialist failure | 1 retry | Fallback default result | `decision_code="specialist_failed"` |

Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, rejected candidates, evidence ownership, claim linkage, timeline, payment/refund totals, source precedence, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và giới hạn tài nguyên. Không ghi API key.
