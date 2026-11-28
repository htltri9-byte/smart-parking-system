# tools - script giả lập và đo đạc

Cài phụ thuộc một lần (từ thư mục gốc repo):

```bash
pip install -r tools/requirements.txt
```

| Script | Việc |
|---|---|
| `measure_latency.py` | Đo độ trễ cảm biến → lệnh mở barrier, xuất CSV (giai đoạn 7) |
| `bench_alpr.py` | Đo độ đúng và p50/p95 của dịch vụ ALPR trên thư mục ảnh có nhãn |
| `collect_samples.py` | Chụp ảnh mẫu từ capture-agent và ghi nhãn biển số |
| `sim_esp32.py` | Giả lập 3 ESP32 (cổng vào, cổng ra, khu đỗ) qua MQTT |
| `sim_gate_cycles.py` | Giả lập N lượt xe vào cổng, có ack servo, sinh dòng `gate_traces` |
| `export_latency.py` | Tổng hợp `gate_traces` thành p50/p95/TB/max, in markdown + ghi CSV |
| `soak_monitor.py` | Theo dõi soak test: online/offline, im lặng ô đỗ, MQTT của backend |
| `chaos_test.py` | Chaos test compose: stop/start từng dịch vụ, đo thời gian healthy, kiểm tra luồng cổng |
| `preflight.py` | Kiểm tra trước buổi demo: container healthy, camera, 3 thiết bị online, DB, một chu trình cổng |

## measure_latency.py - độ trễ cảm biến → barrier

Đo thời gian từ bản tin `parking/gate/in/event` tới lệnh `parking/gate/in/cmd`
có `"barrier":"open"` từ backend (topic theo [docs/CONTRACT.md](../docs/CONTRACT.md)).

```bash
# Tự sinh 30 lượt giả (mặc định), backend + broker phải đang chạy
python tools/measure_latency.py --mode sim

# Chỉ lắng nghe; nhóm tự đưa vật vào cảm biến thật, Ctrl+C để dừng
python tools/measure_latency.py --mode live

# Đổi số lượt / khoảng cách / nơi ghi kết quả
python tools/measure_latency.py --mode sim --turns 50 --gap 8 --csv ket-qua/latency.csv
```

Hai chế độ:

- `--mode sim`: mỗi lượt script publish `gate/in/event` giả, chờ lệnh mở barrier, rồi gửi
  `gate/in/passed` để backend đóng barrier. Nghỉ `--gap` giây giữa hai lượt để backend
  hết thời gian chống trùng (`GATE_COOLDOWN_SECONDS`, mặc định 5 s); đặt `--gap` nhỏ hơn
  sẽ bị bỏ qua lượt và ghi `timeout`.
- `--mode live`: script chỉ subscribe, mỗi `gate/in/event` nhận được là một lượt. Không
  publish gì lên broker.

Đồng hồ: chỉ dùng `time.perf_counter()` của chính script, **không** dùng trường `ts` của
ESP32 (đồng hồ board và laptop lệch nhau nên không trừ được cho nhau).

Bỏ 2 lượt đầu (làm nóng: model ALPR, kết nối DB, driver camera còn nguội). Kết quả:

- In từng lượt: `event -> open: 340.0 ms`.
- In bảng tổng hợp p50, p95, trung bình, lớn nhất và số lượt lỗi/timeout.
- Ghi `results/latency.csv` (mặc định) với các cột
  `turn,mode,started_at,ms_to_open,ms_to_ack,status,note`.
  File dùng UTF-8 kèm BOM để Excel mở đúng tiếng Việt.

### Script đo được gì

- Độ trễ đầu-cuối phía phần mềm: broker → backend (`gate_flow.py`) → REST `/capture`
  (capture-agent) → REST `/recognize` (ALPR) → ghi `sessions`/`plate_reads` → publish
  `gate/in/cmd open` → về tới script.
- p50/p95/trung bình/lớn nhất trên N lượt, kèm từng lượt trong CSV để vẽ biểu đồ.
- Chế độ `live` đo đúng luồng thật với cảm biến, kể cả khi backend đang chạy ALPR thật.

### Script KHÔNG đo được gì

- **Không đo phần cơ khí.** Mốc kết thúc là lúc nhận *lệnh* mở barrier, không phải lúc
  thanh barrier nâng xong. Thời gian servo quay phải quay video hoặc đo cảm biến riêng.
- **Không đo độ trễ cảm biến HC-SR04 → ESP32.** Chế độ `sim` bắt đầu tính từ lúc script
  publish; chế độ `live` bắt đầu từ lúc script *nhận* event, tức muộn hơn thời điểm
  backend nhận vài ms (broker gửi song song cho cả hai). Muốn đo phía board phải đo trên
  chính ESP32.
- **Không dùng được `ts` của ESP32** để tính, vì đồng hồ hai bên lệch nhau; mọi số đo là
  hiệu hai mốc `perf_counter` trong cùng tiến trình.
- **Chưa có topic ack trong hợp đồng**, nên cột `ms_to_ack` để trống. Muốn đo thêm thì
  tự thêm `--ack-topic <topic>`; khi đó script ghi chú `không nhận ack` cho lượt không có
  bản tin xác nhận.
- **Số đo phụ thuộc dịch vụ phụ.** Nếu capture-agent hoặc ALPR tắt, backend vẫn mở barrier
  theo nhánh "không đọc được biển", nên số đo là nhánh nhanh và không đại diện luồng ALPR
  đầy đủ. Muốn số đo thật, chạy đủ `capture-agent` + service `alpr` trước.
- **Không kiểm tra barrier đang mở sẵn.** Backend vẫn publish `open` khi barrier đã mở, nên
  lượt đó vẫn có số đo dù không có chuyển động thật.
- **Không đo tải đồng thời.** Mỗi lượt đo tuần tự; muốn đo khi nhiều xe cùng lúc phải tự
  chạy nhiều tiến trình và tự diễn giải.

## sim_gate_cycles.py - giả lập N lượt xe qua cổng

```bash
# 30 lượt nhưng chỉ nên chạy thử 5 lượt trước (mỗi lượt tốn vài trăm ms ALPR)
python tools/sim_gate_cycles.py --turns 5 --gap 8 --servo-ms 300
```

Mỗi lượt script: publish `gate/in/event` → chờ `gate/in/cmd` (`barrier=open`) → đợi
`--servo-ms` (giả lập servo quay) → publish `gate/in/ack` (`barrier_opened`) → publish
`gate/in/passed`.

- Cần backend + Mosquitto đang chạy và **capture-agent đang chạy với nguồn video**
  (`CAMERA_SOURCE=<file.mp4>`) để ALPR chạy thật. Mỗi lượt backend ghi một dòng `gate_traces`.
- `--gap` phải lớn hơn `GATE_COOLDOWN_SECONDS` (mặc định 5 s), nếu không backend bỏ qua lượt.
- Lượt đầu tiên không nhận được lệnh mở barrier thì script dừng với mã 1 (backend/broker chưa đúng);
  các lượt sau bị timeout chỉ bị đếm vào số lỗi.
- Script **không** đo gì cả; số đo nằm trong backend. Xem bằng `/api/traces` hoặc
  `python tools/export_latency.py`.

## export_latency.py - bảng tổng hợp độ trễ

```bash
python tools/export_latency.py
python tools/export_latency.py --url http://localhost:8000 --limit 200 --warmup 2
```

- Gọi `GET /api/traces.csv?limit=...`, sắp xếp theo `id` tăng dần (API trả mới nhất trước)
  rồi bỏ `--warmup` lượt đầu (mặc định 2).
- In bảng markdown: p50, p95, trung bình, lớn nhất cho `capture_ms`, `alpr_ms`,
  `decide_ms`, `barrier_ms`, `total_ms`; kèm tỉ lệ lượt chặng ALPR dưới 500 ms.
- Ghi `results/latency_summary.csv` (UTF-8 BOM, mở được bằng Excel) với cột
  `metric,n,p50_ms,p95_ms,mean_ms,max_ms,under_500ms_pct`; cột `under_500ms_pct` chỉ có
  giá trị ở dòng `alpr_ms`.
- Chặng nào thiếu mốc (ví dụ chưa nhận ack nên không có `barrier_ms`) thì bị bỏ khỏi
  thống kê của chặng đó, cột `n` cho biết số lượt thực dùng.

## bench_alpr.py - đo chất lượng ALPR

```bash
python tools/bench_alpr.py --dir anh-mau
python tools/bench_alpr.py --dir anh-mau --url http://localhost:8001 --summary results/summary.csv
```

- Đọc `labels.csv` (`filename,plate,note`). `plate` để trống nếu ảnh không có biển;
  `note` là nhóm điều kiện (ví dụ `sang`, `toi`, `lech`, `2dong`), không phân biệt hoa thường.
- In: tỉ lệ đúng/hợp lệ, p50/p95, tỉ lệ dưới 500 ms, **bảng theo nhóm note** (số ảnh, tỉ lệ
  đúng, tỉ lệ hợp lệ, p50/p95), và **các cặp nhầm lẫn ký tự** dạng `ký tự đúng -> ký tự đọc sai`.
- Ghi `results/alpr_bench.csv` (từng ảnh, thêm cột `note` và `confusions`) và
  `results/alpr_summary.csv` (theo nhóm). Đổi chỗ ghi bằng `--csv` / `--summary`.
- Nhầm lẫn ký tự chỉ tính cho ảnh đọc sai mà vẫn ra biển hợp lệ; căn hai biển bằng
  `difflib` nên không báo nhầm ký tự thừa/thiếu thành cặp nhầm.

## soak_monitor.py - theo dõi soak test

```bash
python tools/soak_monitor.py --minutes 30
python tools/soak_monitor.py --minutes 2 --url http://localhost:8000
```

Subscribes `parking/#` và theo dõi `gate_in`, `gate_out`, `slots`:

- **Rớt kết nối:** mỗi bản tin `offline` trên `parking/device/<id>/status` là một lần rớt.
- **Im lặng của ô đỗ:** khoảng cách giữa hai bản tin `parking/slot/<n>/state` liên tiếp;
  lớn hơn **15 s** thì cảnh báo ngay ra màn hình (firmware gửi mỗi 5 s).
- **Backend:** mỗi 30 s gọi `GET /api/health`; thời gian `mqtt_connected=false` được cộng dồn
  (lần gọi lỗi được đếm riêng, không tính vào thời gian đó vì không biết trạng thái).
- **Im lặng dài nhất** của mỗi thiết bị = khoảng cách lớn nhất giữa các bản tin *dữ liệu*
  liên tiếp (bản tin status không tính). Thiết bị không gửi bản tin nào trong phiên được
  tính bằng cả thời gian chạy; khoảng cuối kỳ (từ bản tin cuối tới lúc dừng) không tính.
- Kết thúc in bảng tóm tắt và ghi `results/soak_summary.csv` dạng dài
  `metric,device,value` (`duration_s`, `messages`, `drops`, `longest_silence_s`,
  `slot_gap_alerts`, `health_polls`, `health_errors`, `mqtt_down_s`).
- **Mã thoát:** 1 nếu có thiết bị rớt quá 2 lần; 2 nếu không kết nối được broker.

## chaos_test.py - chaos test Docker Compose

```bash
docker compose up -d            # môi trường phải đang healthy trước
python tools/chaos_test.py
python tools/chaos_test.py --services backend,portal --health-timeout 120
```

- Lần lượt `docker compose stop` rồi `docker compose start` từng dịch vụ (mặc định cả 5:
  `mosquitto,postgres,backend,alpr,portal`), đo thời gian stop và thời gian từ lúc start
  tới khi healthcheck báo `healthy`.
- Sau khi healthy, chờ `--settle` giây (cho backend nối lại MQTT) rồi kiểm tra luồng cổng:
  publish `parking/gate/in/event` và chờ `parking/gate/in/cmd` với `barrier=open`.
- Nghỉ `--gate-gap` giữa hai lần kiểm tra (backend bỏ qua event trùng trong 5 s).
- In bảng markdown `| Dịch vụ | Stop (s) | Healthy sau start (s) | Luồng cổng | Ghi chú |`.
- Mã thoát: `1` nếu có bước thất bại, `2` nếu môi trường chưa healthy hoặc không có compose.
- Không đổi cổng/tên dịch vụ; script chỉ gọi `docker compose` và `docker inspect`.

## preflight.py - kiểm tra trước buổi demo

```bash
python tools/preflight.py
python tools/preflight.py --url http://localhost:8000 --capture-url http://localhost:8002
```

Chạy sau khi `docker compose up -d` và `python capture-agent/app.py`; in PASS/FAIL cho 5 mục:

1. **containers** - `docker compose ps` + `docker inspect` cho `mosquitto`, `postgres`,
   `backend`, `alpr`, `portal`: mọi dịch vụ phải `healthy` (hoặc `running` nếu không có healthcheck).
2. **capture-agent** - `GET /health`: `camera_ok` phải `true`; in kèm `frame_age_s`, nguồn ảnh
   và `last_error` khi hỏng.
3. **devices** - đọc bản tin **retained** `parking/device/<id>/status` cho `gate_in`,
   `gate_out`, `slots`: thiếu status hoặc status `offline` là FAIL. Chờ tối đa
   `--retained-wait` giây (mặc định 3).
4. **database** - `GET /api/health` của backend: cần HTTP 200 và `db_connected: true`
   (503/`false` là FAIL).
5. **simulator** - một chu trình thật qua MQTT: publish `parking/gate/in/event` → chờ
   `gate/in/cmd` `barrier=open` → publish `gate/in/ack` (`barrier_opened`) và
   `gate/in/passed` → chờ `gate/in/cmd` `barrier=close`. In độ trễ open/close.

- Mã thoát `1` nếu có mục FAIL, `0` khi tất cả PASS - dùng được trong CI hoặc trước giờ demo.
- Mỗi mục FAIL in thêm dòng gợi ý xử lý nhanh (bật lại compose, chạy capture-agent, xem log backend...).
- Một mục lỗi không làm dừng các mục còn lại: luôn in đủ 5 dòng để biết phải sửa gì.
- Chu trình thử tạo một phiên thật trong DB (và một dòng `gate_traces`); chạy trước khi lấy
  số liệu chính thức để lượt này không lẫn vào thống kê.
- Kịch bản demo và chỗ điền số liệu: [docs/DEMO_SCRIPT.md](../docs/DEMO_SCRIPT.md).
