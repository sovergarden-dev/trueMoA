# True MoA (Cổng Bảo Vệ Nghiêm Ngặt Cho Mixture of Agents)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Hermes Plugin](https://img.shields.io/badge/Hermes-Plugin-blueviolet.svg)](https://hermes-agent.nousresearch.com)
[![Version](https://img.shields.io/badge/version-1.7.1-green.svg)](CHANGELOG.md)
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
6. **Tối ưu Prefix Cache & Thống kê chi phí từng Reference (v1.7.0):**
   - **Đo & chẩn đoán cache cố vấn (`/true-moa cache`):** Ghi nhận số token uncached, cache read, output, reasoning theo từng cố vấn mà không lưu nội dung prompt; tự chẩn đoán nguyên nhân trượt cache (`miss_prefix_shifted`, `miss_ttl_expired`, `miss_model_switched`, `miss_provider_relay`).
   - **Điều phối cố vấn thông minh (tùy chọn `adaptive_fanout`):** Luôn gọi cố vấn ở câu chat mới của bạn, bỏ qua các bước đọc tool cơ học lặp lại, và gọi lại ngay khi gặp lỗi tool, nén ngữ cảnh, thay đổi lớn, giữ ấm TTL hoặc đạt giới hạn bước.
   - **Cắt ngữ cảnh theo lô giữ Prefix & Lời khuyên cô đọng:** Cắt lịch sử vượt cửa sổ theo lô 25% có ghim điểm đầu để giữ nguyên KV prefix cache, kèm tùy chọn chèn hướng dẫn tĩnh 3 phần.
   - **Chặn trả lời rỗng/từ chối & Giữ ấm model dự phòng:** Coi HTTP 200 rỗng hoặc câu từ chối thiếu dữ liệu là lỗi để thử lại và ghim dự phòng khỏe theo TTL.
7. **Giao diện song ngữ (`English` / `Tiếng Việt`), Chẩn đoán sức khỏe & Điều khiển trực tiếp:**
   - Hỗ trợ các lệnh: `/true-moa status`, `/true-moa smart`, `/true-moa cache`, `/true-moa doctor`, `/true-moa lang [en|vi]`, `/true-moa on`, `/true-moa off`, `/true-moa report`.
   - Trang cài đặt song ngữ tương tác trực quan ngay trong **Settings > Plugins > True MoA** (mặc định `English`, chuyển sang `Tiếng Việt` bằng một cú nhấp), tích hợp thẻ kiểm tra sức khỏe `Doctor`, thẻ KPI cache cố vấn 90 ngày, phím tắt `Ctrl+K` và nhãn trạng thái tùy chọn ở thanh dưới cùng.

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
| **`language`** | `string` | `"en"` | Ngôn ngữ hiển thị trên trang Settings, lệnh `/true-moa`, CLI, chẩn đoán `doctor` và thông báo dừng an toàn (`"en"` = tiếng Anh mặc định, `"vi"` = tiếng Việt). |
| **`enabled`** | `boolean` | `true` | Bật/tắt cổng bảo vệ nghiêm ngặt. Khi tắt, hệ thống quay về cơ chế fail-open mặc định. |
| **`sync_hermes_retries`** | `boolean` | `false` | Khi bật, tự động lấy số lần thử theo `API Retries` của Hermes và tự sinh ladder tương ứng. |
| **`custom_retries`** | `number` | `0` | Số lần thử Reference tùy chỉnh (ví dụ: `5`, `13`). Ưu tiên cao nhất khi $> 0$. Đặt `0` để dùng Sync hoặc Manual. |
| **`block_aggregator_fallback`** | `boolean` | `true` | Chặn đứng việc auxiliary fallback dùng Aggregator trả lời thay cho Reference khi gặp lỗi/rate-limit. |
| **`max_wait_budget_s`** | `number` | `0` | Trần tổng thời gian ngủ giữa các lần retry (giây). `0` = dùng hết thang retry. (Không hủy request đang chạy). |
| **`enable_secondary_fallback`** | `boolean` | `false` | **Tùy chọn (mặc định TẮT).** Khi Reference chính lỗi liên tiếp, gate chuyển sang Reference dự phòng cho các lượt retry còn lại; cổng vẫn giữ nguyên fail-closed. |
| **`secondary_reference_model`** | `string` | `""` | Model dự phòng dạng `provider:model` hoặc danh sách dự phòng nhiều nấc phân tách dấu phẩy (`p1:m1, p2:m2`). Bỏ qua khi công tắc tắt hoặc chuỗi rỗng. |
| **`fallback_trigger_attempts`** | `number` | `2` | Số lượt lỗi của Reference chính trước khi chuyển mạch (giới hạn `1..10`). |
| **`fallback_sticky_s`** | `number` | `300` | Số giây (`0..3600`, `0` = tắt) tiếp tục giữ đường đi thẳng qua model dự phòng khỏe sau khi chuyển mạch để giữ ấm prompt cache. |
| **`min_healthy_references`** | `number` | `0` | `0` (mặc định) bắt buộc **mọi** Reference đều phải thành công. `1..8` bật chế độ Quorum: sau khi hết retry/fallback, chấp nhận tổng hợp từ `>= K` cố vấn khỏe mà không lưu cache lượt suy giảm (tuyệt đối không cho phép `0` Reference). |
| **`adaptive_fanout`** | `boolean` | `false` | **Tùy chọn (mặc định TẮT).** Luôn gọi cố vấn ở câu chat mới của bạn, bỏ qua bước tool cơ học lặp lại, và gọi lại ngay khi gặp lỗi tool, nén ngữ cảnh, delta lớn hoặc giữ ấm TTL. |
| **`adaptive_max_interval`** | `number` | `5` | Số bước tool liên tiếp tối đa (`2..20`) trước khi Điều phối thông minh bắt buộc gọi lại cố vấn. |
| **`adaptive_cache_keepalive_s`** | `number` | `240` | Số giây nghỉ tối đa (`0..3600`, `0` = tắt) trước khi tự động làm mới cố vấn trong vòng lặp dài để giữ ấm TTL 5 phút. |
| **`chunked_context_trim`** | `boolean` | `true` | Cắt bớt lịch sử vượt cửa sổ theo lô ~25% và ghim điểm cắt đầu để giữ byte-exact prefix cache qua nhiều lượt. |
| **`concise_advisory`** | `boolean` | `false` | **Tùy chọn (mặc định TẮT).** Nối chỉ dẫn tĩnh 3 mục vào system prompt của Reference để giảm mạnh token output/reasoning mà không phá prefix cache. |
| **`guard_empty_responses`** | `boolean` | `true` | Chặn câu trả lời rỗng hoặc câu từ chối thiếu dữ liệu trên HTTP 200 như một lỗi có thể thử lại. |
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
- `/true-moa status` — Kiểm tra trạng thái cổng, thang retry đang dùng, chế độ tối ưu cache và bộ đếm lỗi.
- `/true-moa smart [on|off | concise on|off | trim on|off | empty on|off | interval <N> | keepalive <S> | sticky <S>]` — Xem hoặc bật/tắt nhanh tối ưu bộ nhớ đệm & chi phí.
- `/true-moa cache` — Xem tỷ lệ trúng prefix cache 90 ngày, token output/reasoning và chẩn đoán nguyên nhân trượt cache từng cố vấn.
- `/true-moa lang [en | vi]` — Xem hoặc chuyển đổi ngôn ngữ hiển thị giữa Tiếng Anh (`en`, mặc định) và Tiếng Việt (`vi`).
- `/true-moa on` / `/true-moa off` — Bật hoặc tạm tắt cổng bảo vệ ngay lập tức.
- `/true-moa ladder [sync | custom <N> | 10,20,40]` — Xem hoặc cập nhật nhanh thang retry.
- `/true-moa fallback [off | [<attempts>] <provider:model[,...]>]` — Xem, bật hoặc tắt nhanh chuỗi model dự phòng.
- `/true-moa quorum [all | <1..8>]` — Yêu cầu toàn bộ Reference (`all`) hoặc tối thiểu `K` cố vấn khỏe sau retry.
- `/true-moa doctor` — Chẩn đoán tĩnh toàn bộ trạng thái seam, vệ sinh môi trường PM, preset, khóa xác thực và hiệu quả cache mà không tốn token.
- `/true-moa report` — Xem nhật ký các sự cố retry/outage gần nhất (tự động giữ vòng đời 90 ngày).
- `/true-moa retry` — Buộc xác minh lại và bọc lại 9 điểm can thiệp (seams).

Từ cửa sổ dòng lệnh Terminal:
```bash
hermes true-moa status
hermes true-moa smart on
hermes true-moa cache
hermes true-moa doctor
hermes true-moa lang vi
hermes true-moa lang en
hermes true-moa on
hermes true-moa off
hermes true-moa ladder 10,20,40
hermes true-moa fallback 2 bdd:gemini-3.8-flash
hermes true-moa quorum 2
```

---

<a id="architecture"></a>
## 9 Điểm Can Thiệp Kỹ Thuật (Seams & Guarantees)

True MoA can thiệp chính xác vào 9 điểm nút trong luồng xử lý của Hermes Agent:

| Điểm can thiệp (Seam) | Quy tắc & Đảm bảo vận hành |
| :--- | :--- |
| `MoAChatCompletions.prepare` | Bắt lỗi outage hoặc tín hiệu hủy người dùng trong quá trình dựng request để kết thúc lượt sạch sẽ. |
| `MoAChatCompletions.create` | Kích hoạt `MoaReferenceOutage` hoặc `InterruptedError` bên trong retry loop chuẩn của Hermes. |
| `MoAChatCompletions._run_fanout` | Chỉ retry các slot lỗi, giữ ấm model dự phòng khỏe theo TTL, chặn câu rỗng/từ chối, ghi viễn trắc từng cố vấn, thoát ngủ tức thì khi Stop và xóa sạch cache lỗi. |
| `MoAChatCompletions._build_guidance` | Đảm bảo danh sách tham chiếu toàn bộ lỗi không bao giờ chuyển thành văn bản chỉ dẫn. |
| `MoAChatCompletions._fanout_cache_key` | Thực thi Điều phối thông minh (Smart Adaptive Fan-Out) bỏ qua bước đọc cơ học thừa nhưng tự làm mới ngay khi có lỗi tool/nén ngữ cảnh/hết hạn TTL. |
| `moa_loop._trim_messages_for_reference` | Nối hướng dẫn cô đọng tĩnh (nếu bật) và cắt lịch sử vượt cửa sổ theo lô 25% có ghim điểm đầu để bảo toàn prefix cache. |
| `moa_loop.aggregate_moa_context` | Áp dụng đầy đủ cơ chế retry chọn lọc, fallback dự phòng và fail-closed cho lệnh một lượt `/moa`. |
| `turn_api_request.build_api_request` | Chặn đứng nhánh `/moa` một lượt trước khi gọi model chính nếu toàn bộ Reference cạn thang thử. |
| `auxiliary_client._try_main_agent_model_fallback` | Từ chối fallback cho task `moa_reference` (`(None, None, "")`). |

### Chạy Bộ Kiểm Thử Độc Lập

Dự án đi kèm bộ kiểm thử toàn diện **71 tiêu chí bảo vệ**, kiểm tra offline hoàn toàn mà không tốn token hay gọi mạng:

```bash
# Chuẩn bị một lần (Linux/macOS: python3.11 + .venv/bin/; Windows: py -3.11 + .venv\Scripts\)
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt "ruamel.yaml>=0.18,<0.19"

# Kiểm tra lint, kiểu tĩnh và hợp đồng độc lập — đúng các bước CI, không cần lõi Hermes
.venv/bin/ruff check .
.venv/bin/mypy true_moa tests/test_static_contract.py
.venv/bin/python -m pytest tests/test_static_contract.py -q -rs

# Bộ chấp nhận trực tiếp 71 tiêu chí — cần lõi Hermes trên PYTHONPATH
PYTHONPATH=<hermes-workspace> .venv/bin/python selftest.py
PYTHONPATH=<hermes-workspace> .venv/bin/python -m pytest tests/ -q -rs
```

`<hermes-workspace>` là thư mục chứa `hermes_cli/` và `agent/`. Bộ trực tiếp còn cần `ruamel.yaml` vì lõi Hermes import module này lúc khởi động; package được cài ở trên vì đó là phụ thuộc của lõi, không phải của plugin.

GitHub Actions chạy `ruff check .`, `mypy` và `tests/test_static_contract.py` trên cả Linux lẫn Windows (`Python 3.11` & `3.14`) — CI không thu thập bộ trực tiếp nên không có bài nào bị bỏ qua. Ở máy local, `pytest tests/` khi thiếu `PYTHONPATH` sẽ bỏ qua bài chấp nhận trực tiếp kèm lý do rõ ràng; các bài hợp đồng vẫn chạy.

Kết quả chuẩn của bộ trực tiếp:
```text
...
all 71 guarantees held — recovery validation passed
```

---

## Bảo mật & Quyền riêng tư

True MoA duy trì tệp nhật ký viễn trắc chỉ thêm (append-only) tại `<HERMES_HOME>/plugin-data/true-moa/reports/gate-YYYYMMDD.jsonl` (tách biệt khỏi thư mục mã nguồn plugin để không làm biến động dấu vân tay môi trường PM của Hermes):
- Chỉ ghi nhận: nhãn thời gian, tên sự kiện, nhãn slot model, số lần thử và thời gian chờ.
- **Tuyệt đối KHÔNG BAO GIỜ ghi nhận:** Nội dung prompt của người dùng, văn bản phản hồi, hay khóa API/token xác thực.
- Thư mục nhật ký được chặn bởi `.gitignore` để bảo vệ dữ liệu người dùng khi triển khai.

---

## Đóng góp mã nguồn

Mọi đóng góp nhằm hoàn thiện dự án đều được chào đón! Vui lòng đọc [CONTRIBUTING.md](CONTRIBUTING.md) và [SECURITY.md](SECURITY.md) trước khi tạo Pull Request.

---

## Giấy phép (License)

Dự án được phân phối dưới giấy phép mã nguồn mở [MIT License](LICENSE) © 2026 Syrin (sovergarden-dev).
