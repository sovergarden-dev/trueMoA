# True MoA (Cổng Bảo Vệ Nghiêm Ngặt Cho Mixture of Agents)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Hermes Plugin](https://img.shields.io/badge/Hermes-Plugin-blueviolet.svg)](https://hermes-agent.nousresearch.com)
[![Version](https://img.shields.io/badge/version-1.4.0-green.svg)](CHANGELOG.md)
[![Architecture: Fail-Closed](https://img.shields.io/badge/Architecture-Fail--Closed-red.svg)](#architecture)

[ **English** ](README.md) | [ **Tiếng Việt** ](README.vi.md)

---

**True MoA** là plugin độ tin cậy chạy in-process, **không chỉnh sửa mã nguồn lõi** dành cho [Hermes Agent](https://github.com/NousResearch/Hermes-Agent) (`>=0.23.0`).

Plugin thực thi triết lý **Fail-Closed nghiêm ngặt** cho kiến trúc Mixture of Agents (MoA): khi các mô hình tham chiếu (Reference) gặp sự cố hoặc timeout, mô hình tổng hợp (Aggregator) bị chặn hoàn toàn việc tự ý trả lời mồ côi mà không có tri thức tham chiếu; đồng thời các kết quả lỗi không bao giờ bị lưu vào bộ nhớ đệm cache, và ngăn chặn tuyệt đối hành vi Aggregator đóng giả Reference.

---

## Vấn đề: Cơ chế mặc định của MoA là Fail-Open

Trong kiến trúc MoA mặc định của Hermes Agent:
- Nếu một mô hình Reference bị ngắt kết nối hoặc timeout, kết quả trả về bị thay thế bằng chuỗi `[failed: ...]`.
- Mô hình Aggregator được chỉ thị *"hãy tự phán đoán theo ý mình"* (act on your own judgment), âm thầm trả lời người dùng như một mô hình đơn lẻ mà người dùng không hề hay biết tri thức tham chiếu đã mất.
- Các phản hồi lỗi bị lưu vào fan-out cache, làm ô nhiễm các lượt đàm thoại tiếp theo.
- Nếu cơ chế fallback của auxiliary client được bật, hệ thống thậm chí âm thầm dùng chính mô hình Aggregator để trả lời thay cho Reference.

```mermaid
flowchart TD
    subgraph MoA_Mac_Dinh [Hermes MoA Mặc Định: Fail-Open]
        A1[Người dùng gửi Prompt] --> B1[Fan-out gọi các Model Reference]
        B1 -->|Reference lỗi / Timeout| C1[Kết quả lỗi: '[failed: timeout]']
        C1 --> D1[Aggregator tự ý trả lời một mình]
        D1 --> E1[Người dùng nhận kết quả chưa kiểm chứng]
        C1 -.-> F1[Fan-out Cache bị ô nhiễm lỗi]
    end

    subgraph True_MoA_Gate [True MoA: Fail-Closed & Đã Xác Minh]
        A2[Người dùng gửi Prompt] --> B2[Fan-out gọi các Model Reference]
        B2 -->|Reference lỗi| C2{Thang Retry / Đồng bộ còn lượt?}
        C2 -->|Còn lượt thử| D2[Tự động giãn cách ngủ & Thử lại]
        D2 --> B2
        C2 -->|Cạn sạch thang thử| E2[Dừng lượt an toàn]
        E2 --> F2[Fail-Closed: Chặn Aggregator trả lời mồ côi]
        E2 --> G2[Xóa sạch Cache lỗi vô điều kiện]
        B2 -->|Reference thành công| H2[Aggregator tổng hợp tri thức đã kiểm chứng]
    end

    style MoA_Mac_Dinh fill:#ffebee,stroke:#c62828,stroke-width:2px
    style True_MoA_Gate fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
```

---

## Các tính năng cốt lõi

1. **Bảo vệ Fail-Closed nghiêm ngặt (`gate.py`):**
   - Khi toàn bộ Reference model cạn lượt thử trên ladder, lượt đàm thoại sẽ dừng an toàn thông qua error handler chuẩn của Hermes trước khi Aggregator kịp tạo câu trả lời mồ côi.
2. **Chặn Aggregator đóng giả Reference:**
   - Ngăn chặn hoàn toàn cơ chế auxiliary fallback cho task `moa_reference` (`(None, None, "")`). Mô hình Aggregator không bao giờ được phép đóng giả Reference của chính mình.
3. **Thang Retry tự thích ứng & Đồng bộ Hermes (v1.2.0):**
   - **Chế độ Đồng bộ (Sync):** Tự động kế thừa số lần thử từ cấu hình Hermes (`agent.api_max_retries`, ví dụ 13 lần) và tự động sinh thang nghỉ lũy tiến (`10s → 15s → 20s → 30s → 45s → 60s...`).
   - **Số lần thử tùy chỉnh (Custom Counter):** Nhập trực tiếp số lần muốn thử (ví dụ: `5` hoặc `13`) mà không cần gõ mảng thủ công.
   - **Thang nghỉ thủ công (Manual Ladder):** Cho phép tự cấu hình từng nhịp giây nghỉ theo ý muốn.
4. **Bảo vệ Cache sạch tuyệt đối:**
   - Bất kỳ kết quả nào dính chuỗi `[failed: ...]` đều lập tức kích hoạt xóa sạch `_ref_cache_outputs` và `_ref_cache_key`, chống lây lan kết quả lỗi sang các turn kế tiếp.
5. **Không chỉnh sửa mã nguồn lõi (Zero Core Edits):**
   - Hoạt động 100% như một in-process plugin thông qua wrapping signature-verified. Cập nhật Hermes an toàn mà không sợ xung đột mã nguồn.
6. **Nhật ký viễn trắc & Lệnh điều khiển trực tiếp:**
   - Hỗ trợ các lệnh: `/true-moa status`, `/true-moa on`, `/true-moa off`, `/true-moa report`.
   - Giao diện trực quan ngay trong **Settings > Plugins > true-moa**.

---

## Cài đặt

### Cách 1: Cài qua Hermes Plugin CLI (Khuyên dùng)

```bash
hermes plugins install https://github.com/sovergarden-dev/trueMoA
```

Kích hoạt plugin:
```bash
hermes plugins enable true-moa
```

### Cách 2: Cài đặt thủ công

Sao chép kho lưu trữ này vào thư mục plugins của Hermes:

**Trên Windows:**
```powershell
git clone https://github.com/sovergarden-dev/trueMoA.git "$env:LOCALAPPDATA\hermes\plugins\true-moa"
hermes plugins enable true-moa
```

**Trên Linux / macOS:**
```bash
git clone https://github.com/sovergarden-dev/trueMoA.git ~/.hermes/plugins/true-moa
hermes plugins enable true-moa
```

Kiểm tra trạng thái sau cài đặt:
```bash
hermes true-moa status
```

---

## Cấu hình

True MoA đồng bộ 2 chiều hoàn hảo giữa giao diện Desktop (**Settings > Plugins > true-moa**), lệnh chat slash, và file `config.yaml`:

| Tham số | Kiểu | Mặc định | Mô tả |
| :--- | :---: | :---: | :--- |
| **`enabled`** | `boolean` | `true` | Bật/tắt cổng bảo vệ nghiêm ngặt. Khi tắt, hệ thống quay về cơ chế fail-open mặc định. |
| **`sync_hermes_retries`** | `boolean` | `false` | Khi bật, tự động lấy số lần thử theo `API Retries` của Hermes và tự sinh ladder tương ứng. |
| **`custom_retries`** | `number` | `0` | Số lần thử Reference tùy chỉnh (ví dụ: `5`, `13`). Ưu tiên cao nhất khi $> 0$. Đặt `0` để dùng Sync hoặc Manual. |
| **`block_aggregator_fallback`** | `boolean` | `true` | Chặn đứng việc auxiliary fallback dùng Aggregator trả lời thay cho Reference khi gặp lỗi/rate-limit. |
| **`max_wait_budget_s`** | `number` | `0` | Trần tổng thời gian ngủ giữa các lần retry (giây). `0` = dùng hết thang retry. (Không hủy request đang chạy). |
| **`enable_secondary_fallback`** | `boolean` | `false` | **Tùy chọn (mặc định TẮT).** Khi Reference chính lỗi liên tiếp, gate chuyển sang Reference dự phòng cho các lượt retry còn lại; cổng vẫn giữ nguyên fail-closed. |
| **`secondary_reference_model`** | `string` | `""` | Model dự phòng dạng `provider:model` hoặc danh sách dự phòng nhiều nấc phân tách dấu phẩy (`p1:m1, p2:m2`). Bỏ qua khi công tắc tắt hoặc chuỗi rỗng. |
| **`fallback_trigger_attempts`** | `number` | `2` | Số lượt lỗi của Reference chính trước khi chuyển mạch (giới hạn `1..10`). |
| **`ladder`** | `list` | `[15, 30, 60, 60, 60]` | Mảng số giây nghỉ thủ công giữa các lần thử lại Reference (dùng khi Sync và Custom đều tắt). |

### Thứ tự ưu tiên cấu hình:
$$\text{custom\_retries} > 0 \;\longrightarrow\; \text{sync\_hermes\_retries: true} \;\longrightarrow\; \text{mảng ladder thủ công}$$

### Reference Dự Phòng (tùy chọn, mặc định TẮT)

Khi bật `enable_secondary_fallback` và Reference chính lỗi quá `fallback_trigger_attempts` lượt, gate thay bằng `secondary_reference_model` cho các nhịp retry còn lại, đồng thời ghi sự kiện `reference_fallback_switched` vào telemetry. Các Reference khỏe vẫn được giữ lại.

Việc chuyển mạch **không hề nới lỏng fail-closed**:

- Nếu Reference dự phòng cũng lỗi, lượt thoại vẫn dừng — Aggregator không được trả lời một mình.
- Model dự phòng trùng Aggregator (hoặc dùng provider `moa`) bị từ chối để ngăn Aggregator đóng giả Reference.
- Kết quả lỗi vẫn không bao giờ được đưa vào cache.

---

## Lệnh điều khiển (CLI & Slash Commands)

Trong bất kỳ phiên chat nào của Hermes Desktop:
- `/true-moa status` — Kiểm tra trạng thái cổng, thang retry đang dùng, nguồn cấu hình và bộ đếm lỗi.
- `/true-moa on` / `/true-moa off` — Bật hoặc tạm tắt cổng bảo vệ ngay lập tức.
- `/true-moa ladder [sync | custom <N> | 10,20,40]` — Xem hoặc cập nhật nhanh thang retry.
- `/true-moa fallback [off | [<attempts>] <provider:model[,...]>]` — Xem, bật hoặc tắt nhanh chuỗi model dự phòng.
- `/true-moa report` — Xem nhật ký các sự cố retry/outage gần nhất (tự động giữ vòng đời 90 ngày).
- `/true-moa retry` — Buộc xác minh lại và bọc lại 7 điểm can thiệp (seams).

Từ cửa sổ dòng lệnh Terminal:
```bash
hermes true-moa status
hermes true-moa on
hermes true-moa off
hermes true-moa ladder 10,20,40
hermes true-moa fallback 2 bdd:gemini-3.8-flash
```

---

<a id="architecture"></a>
## 7 Điểm Can Thiệp Kỹ Thuật (Seams & Guarantees)

True MoA can thiệp chính xác vào 7 điểm nút trong luồng xử lý của Hermes Agent:

| Điểm can thiệp (Seam) | Quy tắc & Đảm bảo vận hành |
| :--- | :--- |
| `MoAChatCompletions.prepare` | Bắt lỗi outage hoặc tín hiệu hủy người dùng trong quá trình dựng request để kết thúc lượt sạch sẽ. |
| `MoAChatCompletions.create` | Kích hoạt `MoaReferenceOutage` hoặc `InterruptedError` bên trong retry loop chuẩn của Hermes. |
| `MoAChatCompletions._run_fanout` | Chỉ retry các slot lỗi, chuyển tiếp chuỗi dự phòng nhiều nấc, thoát ngủ tức thì khi bấm Stop và xóa sạch cache lỗi. |
| `MoAChatCompletions._build_guidance` | Đảm bảo danh sách tham chiếu toàn bộ lỗi không bao giờ chuyển thành văn bản chỉ dẫn. |
| `moa_loop.aggregate_moa_context` | Áp dụng đầy đủ cơ chế retry chọn lọc, fallback dự phòng và fail-closed cho lệnh một lượt `/moa`. |
| `turn_api_request.build_api_request` | Chặn đứng nhánh `/moa` một lượt trước khi gọi model chính nếu toàn bộ Reference cạn thang thử. |
| `auxiliary_client._try_main_agent_model_fallback` | Từ chối fallback cho task `moa_reference` (`(None, None, "")`). |

### Chạy Bộ Kiểm Thử Độc Lập

Dự án đi kèm bộ kiểm thử toàn diện **51 tiêu chí bảo vệ**, kiểm tra offline hoàn toàn mà không tốn token hay gọi mạng:

```bash
# Chuẩn bị một lần (Linux/macOS: python3.11 + .venv/bin/; Windows: py -3.11 + .venv\Scripts\)
python3.11 -m venv .venv
.venv/bin/pip install "ruff==0.17.5" "pyyaml==6.0.3" "pytest==8.3.4" "ruamel.yaml>=0.18,<0.19"

# Kiểm thử hợp đồng độc lập — đúng lệnh CI, không cần lõi Hermes
.venv/bin/python -m pytest tests/test_static_contract.py -q -rs

# Bộ chấp nhận trực tiếp 44 tiêu chí — cần lõi Hermes trên PYTHONPATH
PYTHONPATH=<hermes-workspace> .venv/bin/python selftest.py
PYTHONPATH=<hermes-workspace> .venv/bin/python -m pytest tests/ -q -rs
```

`<hermes-workspace>` là thư mục chứa `hermes_cli/` và `agent/`. Bộ trực tiếp còn cần `ruamel.yaml` vì lõi Hermes import module này lúc khởi động; package được cài ở trên vì đó là phụ thuộc của lõi, không phải của plugin.

GitHub Actions chạy `ruff check .` và **chỉ** `tests/test_static_contract.py` — CI không thu thập bộ trực tiếp nên không có bài nào bị bỏ qua. Ở máy local, `pytest tests/` khi thiếu `PYTHONPATH` sẽ bỏ qua bài chấp nhận trực tiếp kèm lý do rõ ràng; các bài hợp đồng vẫn chạy.

Kết quả chuẩn của bộ trực tiếp:
```text
...
all 51 guarantees held — recovery validation passed
```

---

## Bảo mật & Quyền riêng tư

True MoA duy trì tệp nhật ký viễn trắc chỉ thêm (append-only) tại `reports/gate-YYYYMMDD.jsonl`:
- Chỉ ghi nhận: nhãn thời gian, tên sự kiện, nhãn slot model, số lần thử và thời gian chờ.
- **Tuyệt đối KHÔNG BAO GIỜ ghi nhận:** Nội dung prompt của người dùng, văn bản phản hồi, hay khóa API/token xác thực.
- Thư mục nhật ký được chặn bởi `.gitignore` để bảo vệ dữ liệu người dùng khi triển khai.

---

## Đóng góp mã nguồn

Mọi đóng góp nhằm hoàn thiện dự án đều được chào đón! Vui lòng đọc [CONTRIBUTING.md](CONTRIBUTING.md) và [SECURITY.md](SECURITY.md) trước khi tạo Pull Request.

---

## Giấy phép (License)

Dự án được phân phối dưới giấy phép mã nguồn mở [MIT License](LICENSE) © 2026 Syrin (sovergarden-dev).
